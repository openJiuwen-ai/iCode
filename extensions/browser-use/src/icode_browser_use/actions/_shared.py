# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Private helpers shared by the ten browser action modules.

Everything here is an implementation detail of ``actions``; the public surface
is ``actions.dispatch`` plus each module's ``NAME`` / ``DESCRIPTION`` /
``PARAMS`` / ``run``. The helpers centralise the cross-cutting bits - the
three-source snapshot capture, budgeted rendering, text truncation, small CDP
probes and the conservative login-wall detector - so each action stays a thin
translation of one browser tool into CDP calls.

Imports of :mod:`snapshot.fusion`, :mod:`snapshot.serializer` and
:mod:`policy` are done lazily inside the functions that need them, mirroring
``session.py``: it keeps this package importable on its own (and testable with
fakes) even before those perception/policy modules are wired in.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlsplit

from ..errors import BrowserError, CDPTimeout, ConnectionClosed, LoginRequired
from ..logging import log

#: A login form is "dominant" only on a sparse page (design §8.2, conservative).
_LOGIN_INTERACTIVE_LIMIT = 12

#: How long a write action watches for a JS dialog right after its send boundary.
#: The dialog event races the command response by a few milliseconds; waiting this
#: long keeps the action from reporting an outcome the dialog has already paused.
DIALOG_SETTLE_SECONDS = 0.1

_DIALOG_POLL_SECONDS = 0.02

_PASSWORD_LABEL_RE = re.compile(r"password|passwd|passcode|密码", re.IGNORECASE)
_LOGIN_PATH_RE = re.compile(r"/(?:login|signin|sign-in|passport|sso|auth)(?:/|$)", re.IGNORECASE)


def url_host(url: str) -> str:
    """Return the hostname of *url* (empty when absent)."""
    return urlsplit(url or "").hostname or ""


async def dialog_since(session: Any, seq: int, *, wait: float = DIALOG_SETTLE_SECONDS) -> Any:
    """Return the dialog that opened after *seq*, watching briefly for the event.

    ``seq`` is ``session.dialog_seq`` taken just before the action's send. A
    returned record means the page's script is paused and the action's outcome is
    unproven, even when the record was already auto-dismissed.
    """
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        info = session.dialog_since(seq)
        if info is not None:
            return info
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(_DIALOG_POLL_SECONDS)


def _consume_detached_send(task: asyncio.Task) -> None:
    """Log a send that finished after its action already returned a dialog result."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log(f"a write action's CDP command finished after a dialog opened: {error!r}")


async def send_watching_dialog(session: Any, seq: int, send: Any) -> tuple[Any, Any]:
    """Await *send* unless a JS dialog opens first; return ``(value, dialog)``.

    ``Input.*`` and ``Runtime.callFunctionOn`` responses are held until the page's
    script resumes, so a click that opens ``alert()`` would otherwise block its own
    tool call for the whole dialog timeout and hide the dialog from
    ``browser_dialog``. When that happens the in-flight command is detached (its
    outcome is only logged) and the dialog is returned: the action has been
    dispatched, its effect is unproven.
    """
    task = asyncio.ensure_future(send())
    try:
        while True:
            info = session.dialog_since(seq)
            if info is not None:
                task.add_done_callback(_consume_detached_send)
                return None, info
            if task.done():
                return await task, None
            await asyncio.wait({task}, timeout=DIALOG_SETTLE_SECONDS)
    except asyncio.CancelledError:
        task.cancel()
        raise


def url_origin(url: str) -> str:
    """Return ``scheme://netloc`` of *url* (empty when not absolute)."""
    parts = urlsplit(url or "")
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return ""


def truncate_chars(text: str, limit: int) -> str:
    """Hard-truncate *text* to *limit* code points, with a visible marker."""
    if limit <= 0 or len(text) <= limit:
        return text
    removed = len(text) - limit
    return text[:limit] + f"… (+{removed} more chars)"


def display_url(url: str, limit: int = 200) -> str:
    """An origin+path view of *url* for model-visible listings (credentials, ``;params`` and query stripped)."""
    from ..policy import sanitize_display_url

    return truncate_chars(sanitize_display_url(url), limit)


def stats_line(session: Any, nodes: list[Any]) -> str:
    """One-line node/budget statistics appended to a snapshot result."""
    from ..snapshot.serializer import is_structural

    visible = [node for node in nodes if not is_structural(node)]
    interactive = sum(1 for node in nodes if getattr(node, "interactive", False))
    offscreen = sum(1 for node in visible if not getattr(node, "in_viewport", False))
    parts = [f"interactive {interactive}", f"offscreen {offscreen}"]
    hidden = len(nodes) - len(visible)
    if hidden:
        parts.append(f"structural hidden {hidden}")
    parts.append(f"epoch {session.page_epoch}")
    return " / ".join(parts)


def login_hint(url: str) -> str:
    """A tail note for login/SSO-ish URL paths (a hint, never a trigger)."""
    if _LOGIN_PATH_RE.search(urlsplit(url or "").path or ""):
        return "附注：当前 URL 路径疑似登录/SSO 流程；若页面内容正常可忽略本行。"
    return ""


def snapshot_is_stale(session: Any) -> bool:
    """True when no snapshot has been taken on the current page epoch."""
    nodes = session.snapshot.nodes
    return not nodes or any(node.epoch != session.page_epoch for node in nodes)


def snapshot_needs_refresh(session: Any) -> bool:
    """True when the snapshot is stale (missing/old epoch) or dirtied by a write action."""
    return bool(getattr(session, "snapshot_dirty", False)) or snapshot_is_stale(session)


async def resolve_target(session: Any, spec: str) -> Any:
    """``session.resolve_target`` for an action: free text first refreshes a stale or dirty snapshot.

    A ``@eN#epoch`` reference is never refreshed (staleness is the answer); free
    text means "whatever matches on the page now", so a snapshot the page has
    outgrown (navigation, epoch bump, or an in-page write that marked the
    snapshot dirty) is retaken before ranking. The retake is the bare capture
    (as ``browser_wait_for`` does): login detection and mutation bookkeeping
    stay with ``browser_snapshot``, so an action never flips the session into
    the login-wait state on its own.
    """
    text = str(spec or "").strip()
    if text and not text.startswith("@") and snapshot_needs_refresh(session):
        await capture_and_register(session)
    return session.resolve_target(spec)


async def eval_value(session: Any, expression: str, *, cdp_session_id: str | None = None) -> Any:
    """Run a read-only ``Runtime.evaluate`` returning a JSON value."""
    client = session.require_client()
    response = await client.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=cdp_session_id or session.session_id,
        timeout=session.cfg.evaluate_timeout,
    )
    return response.get("result", {}).get("value")


async def call_value(
    session: Any,
    object_id: str,
    function_declaration: str,
    *,
    cdp_session_id: str | None = None,
    arguments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run a ``Runtime.callFunctionOn`` against *object_id``; return ``result``."""
    client = session.require_client()
    params: dict[str, Any] = {
        "objectId": object_id,
        "functionDeclaration": function_declaration,
        "returnByValue": True,
    }
    if arguments is not None:
        params["arguments"] = arguments
    response = await client.send(
        "Runtime.callFunctionOn",
        params,
        session_id=cdp_session_id or session.session_id,
        timeout=session.cfg.evaluate_timeout,
    )
    return response.get("result") or {}


async def gather_in_order(*awaitables: Any) -> list[Any]:
    """Run *awaitables* together; once all finished, raise the first failure in argument order.

    Unlike a bare ``gather`` nothing is left running after an error (the action lock
    is released only when every request has settled), and the error reported is
    the one the former sequential code would have raised first.
    """
    results = await asyncio.gather(*awaitables, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return list(results)


async def capture_sources(
    session: Any, *, cdp_session_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Capture the ``DOMSnapshot`` plus a best-effort ``Accessibility`` tree."""
    from ..snapshot.fusion import SNAPSHOT_CAPTURE_PARAMS

    client = session.require_client()
    session_id = cdp_session_id or session.session_id
    # Both reads are independent: send them together so they cost one round trip.
    snapshot_result, ax_result = await asyncio.gather(
        client.send(
            "DOMSnapshot.captureSnapshot",
            dict(SNAPSHOT_CAPTURE_PARAMS),
            session_id=session_id,
            timeout=session.cfg.cdp_timeout,
        ),
        client.send(
            "Accessibility.getFullAXTree",
            session_id=session_id,
            timeout=session.cfg.cdp_timeout,
        ),
        return_exceptions=True,
    )
    if isinstance(snapshot_result, BaseException):
        raise snapshot_result
    ax: dict[str, Any] | None
    if isinstance(ax_result, BrowserError):  # the AX tree is an enhancement, not a hard need
        log(f"AX tree unavailable for {session_id}, fusing DOMSnapshot alone: {ax_result!r}")
        ax = None
    elif isinstance(ax_result, BaseException):
        raise ax_result
    else:
        ax = ax_result
    return snapshot_result, ax


_OOPIF_DOMAINS = ("Page.enable", "Runtime.enable", "DOM.enable", "Accessibility.enable")

_IFRAME_ORIGIN_FN = (
    "function(){"
    " let x=0, y=0, el=this;"
    " while(el){"
    "   const r=el.getBoundingClientRect();"
    "   const win=el.ownerDocument && el.ownerDocument.defaultView;"
    "   const cs=(win && win.getComputedStyle) ? win.getComputedStyle(el) : getComputedStyle(el);"
    "   x += r.left + (parseFloat(cs.borderLeftWidth)||0) + (parseFloat(cs.paddingLeft)||0);"
    "   y += r.top + (parseFloat(cs.borderTopWidth)||0) + (parseFloat(cs.paddingTop)||0);"
    "   el = win && win.frameElement ? win.frameElement : null;"
    " }"
    " return {x, y};"
    "}"
)


async def ensure_oopif_domains(session: Any, child_session: str) -> None:
    """Enable CDP domains on *child_session* once per attached session."""
    is_enabled = getattr(session, "is_oopif_enabled", None)
    if callable(is_enabled) and is_enabled(child_session):
        return
    client = session.require_client()
    results = await asyncio.gather(
        *(client.send(domain, session_id=child_session, timeout=session.cfg.cdp_timeout) for domain in _OOPIF_DOMAINS),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, ConnectionClosed):
            raise result
        if isinstance(result, BaseException) and not isinstance(result, BrowserError):
            raise result
    if not any(isinstance(result, CDPTimeout) for result in results):
        mark_enabled = getattr(session, "mark_oopif_enabled", None)
        if callable(mark_enabled):
            mark_enabled(child_session)


def _quad_origin(quad: Any) -> tuple[float, float] | None:
    if not isinstance(quad, (list, tuple)) or len(quad) < 8:
        return None
    xs = [float(v) for v in quad[0::2]]
    ys = [float(v) for v in quad[1::2]]
    return (min(xs), min(ys)) if xs and ys else None


async def _frame_owner_viewport_origin(
    session: Any,
    child_session: str,
    parent_session: str | None,
) -> tuple[float, float] | None:
    """Read *child_session*'s live iframe content-box origin in *parent_session*'s viewport."""
    client = session.require_client()
    frame_id_fn = getattr(session, "oopif_frame_id", None)
    frame_id = frame_id_fn(child_session) if callable(frame_id_fn) else ""
    if not frame_id:
        tree = await client.send(
            "Page.getFrameTree",
            session_id=child_session,
            timeout=session.cfg.cdp_timeout,
        )
        frame = (tree or {}).get("frameTree", {}).get("frame", {}) if isinstance(tree, dict) else {}
        frame_id = str(frame.get("id", "")) if isinstance(frame, dict) else ""
        if frame_id:
            set_frame_id = getattr(session, "set_oopif_frame_id", None)
            if callable(set_frame_id):
                set_frame_id(child_session, frame_id)
    if not frame_id:
        return None
    parent_sid = parent_session or session.session_id
    owner = await client.send(
        "DOM.getFrameOwner",
        {"frameId": frame_id},
        session_id=parent_sid,
        timeout=session.cfg.cdp_timeout,
    )
    owner_backend = int((owner or {}).get("backendNodeId", 0) or 0) if isinstance(owner, dict) else 0
    if not owner_backend:
        return None
    try:
        resolved = await client.send(
            "DOM.resolveNode",
            {"backendNodeId": owner_backend},
            session_id=parent_sid,
            timeout=session.cfg.cdp_timeout,
        )
        owner_object_id = (
            str(((resolved or {}).get("object") or {}).get("objectId", "")) if isinstance(resolved, dict) else ""
        )
        if owner_object_id:
            origin_res = await call_value(
                session,
                owner_object_id,
                _IFRAME_ORIGIN_FN,
                cdp_session_id=parent_session,
            )
            val = origin_res.get("value") if isinstance(origin_res, dict) else None
            if isinstance(val, dict) and "x" in val and "y" in val:
                return float(val["x"]), float(val["y"])
    except ConnectionClosed:
        raise
    except BrowserError:
        pass
    try:
        quads_res = await client.send(
            "DOM.getContentQuads",
            {"backendNodeId": owner_backend},
            session_id=parent_sid,
            timeout=session.cfg.cdp_timeout,
        )
        quads = (quads_res or {}).get("quads") if isinstance(quads_res, dict) else None
        if isinstance(quads, list) and quads:
            origin = _quad_origin(quads[0])
            if origin is not None:
                return origin
    except ConnectionClosed:
        raise
    except BrowserError:
        pass
    return None


async def resolve_oopif_offset(session: Any, cdp_session_id: str | None) -> tuple[float, float]:
    """Read *cdp_session_id*'s live content-box origin in main-frame viewport coordinates.

    Walks the OOPIF ancestor chain up to the main frame after any scroll so
    screenshot, click and hover all share fresh post-scroll coordinates. Falls
    back to the cached ``session.oopif_offset`` when live lookup is unavailable
    (for example in minimal unit-test doubles).
    """
    if not cdp_session_id:
        return (0.0, 0.0)
    offset_fn = getattr(session, "oopif_offset", None)
    cached = offset_fn(cdp_session_id) if callable(offset_fn) else (0.0, 0.0)
    parent_fn = getattr(session, "oopif_parent", None)
    chain: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    current: str | None = cdp_session_id
    while current and current not in seen and len(chain) < 8:
        seen.add(current)
        parent = parent_fn(current) if callable(parent_fn) else None
        chain.append((current, parent))
        current = parent
    chain.reverse()
    total_x = 0.0
    total_y = 0.0
    set_offset = getattr(session, "set_oopif_offset", None)
    for child_sid, parent_sid in chain:
        try:
            origin = await _frame_owner_viewport_origin(session, child_sid, parent_sid)
        except ConnectionClosed:
            raise
        except BrowserError:
            return cached
        if origin is None:
            return cached
        total_x += origin[0]
        total_y += origin[1]
        if callable(set_offset):
            set_offset(child_sid, (total_x, total_y))
    return (total_x, total_y)


async def scroll_into_view_and_offset(
    session: Any,
    object_id: str,
    cdp_session_id: str | None,
) -> tuple[float, float]:
    """Scroll *object_id* into view, then read its OOPIF's live main-viewport offset."""
    client = session.require_client()
    await client.send(
        "DOM.scrollIntoViewIfNeeded",
        {"objectId": object_id},
        session_id=cdp_session_id or session.session_id,
        timeout=session.cfg.cdp_timeout,
    )
    return await resolve_oopif_offset(session, cdp_session_id)


async def _capture_oopif_tree(
    session: Any,
    child_session: str,
    parent_session: str | None,
    parent_nodes: list[Any],
    depth: int,
    budget: list[int],
    offsets: dict[str, tuple[float, float]],
) -> list[Any]:
    """Capture one OOPIF and its descendants, offsetting each by its owner box.

    Offsets are collected in *offsets* and applied by the caller only after the
    snapshot registers, so an interrupted capture cannot leave frame offsets
    that describe a snapshot the session does not hold.
    """
    from dataclasses import replace

    from ..snapshot.fusion import fuse

    if depth > 3 or budget[0] <= 0:
        return []
    client = session.require_client()
    try:
        await ensure_oopif_domains(session, child_session)
        frame = await session.read_frame_metrics(cdp_session_id=child_session)
        if not frame.frame_id:
            return []
        set_frame_id = getattr(session, "set_oopif_frame_id", None)
        if callable(set_frame_id):
            set_frame_id(child_session, frame.frame_id)
        owner = await client.send(
            "DOM.getFrameOwner",
            {"frameId": frame.frame_id},
            session_id=parent_session or session.session_id,
            timeout=session.cfg.cdp_timeout,
        )
        owner_backend = int(owner.get("backendNodeId", 0) or 0)
        owner_node = next((node for node in parent_nodes if node.backend_node_id == owner_backend), None)
        if owner_node is None or owner_node.bbox[2] <= 0 or owner_node.bbox[3] <= 0:
            return []
        snapshot, ax = await capture_sources(session, cdp_session_id=child_session)
        child_nodes = fuse(
            snapshot,
            ax,
            frame=replace(frame, offset=(owner_node.bbox[0], owner_node.bbox[1])),
            target_id=session.target_id,
            cdp_session_id=child_session,
        )
        offsets[child_session] = (owner_node.bbox[0], owner_node.bbox[1])
        budget[0] -= 1
        result = [node for node in child_nodes if node.visible]
        for grandchild in session.oopif_children(child_session):
            result.extend(
                await _capture_oopif_tree(
                    session,
                    grandchild,
                    child_session,
                    child_nodes,
                    depth + 1,
                    budget,
                    offsets,
                )
            )
        return result
    except Exception as exc:  # one iframe must not break the page snapshot
        log(f"OOPIF {child_session} snapshot skipped: {exc!r}")
        return []


async def _capture_oopif_nodes(
    session: Any, parent_nodes: list[Any]
) -> tuple[list[Any], dict[str, tuple[float, float]]]:
    """Fuse the OOPIF tree into the main frame's node list (depth-first)."""
    if not parent_nodes:
        return [], {}
    budget = [6]
    offsets: dict[str, tuple[float, float]] = {}
    result: list[Any] = []
    for child_session in session.oopif_children(None):
        result.extend(await _capture_oopif_tree(session, child_session, None, parent_nodes, 0, budget, offsets))
    return result, offsets


async def capture_and_register(
    session: Any,
    *,
    url: str = "",
    title: str = "",
    dom_changed: bool = False,
) -> tuple[list[Any], Any]:
    """Capture three sources, fuse them, register a snapshot; return nodes+frame."""
    from ..snapshot.fusion import fuse

    (snapshot, ax), frame = await gather_in_order(capture_sources(session), session.read_frame_metrics())
    nodes = fuse(snapshot, ax, frame=frame, target_id=session.target_id)
    oopif_nodes, offsets = await _capture_oopif_nodes(session, nodes)
    nodes.extend(oopif_nodes)
    session.register_snapshot(
        nodes,
        url=url or frame.url or session.snapshot.url,
        title=title or frame.title,
        dom_changed=dom_changed,
    )
    for cdp_session_id, offset in offsets.items():
        session.set_oopif_offset(cdp_session_id, offset)
    return nodes, frame


def render_snapshot(session: Any, nodes: list[Any], *, url: str, title: str, focus: str = "") -> str:
    """Render *nodes* with the budgeted serializer using the session's config."""
    from ..snapshot.serializer import render

    return render(
        nodes,
        page_epoch=session.page_epoch,
        version=getattr(session.snapshot, "version", 0),
        url=url,
        title=title,
        budget=session.cfg.snapshot_char_budget,
        max_nodes=session.cfg.max_stage_nodes,
        focus=focus,
    )


def _is_password_input(node: Any) -> bool:
    """Best-effort ``input[type=password]`` test over a fused node.

    The fused node does not carry the raw ``type`` attribute, so an explicit
    ``type``/``input_type`` is honoured when the fusion layer exposes it, and
    otherwise a password-ish accessible name/placeholder is required - keeping
    the check conservative.
    """
    tag = str(getattr(node, "tag", "") or "").lower()
    if tag != "input":
        return False
    explicit = str(getattr(node, "input_type", "") or getattr(node, "type", "") or "").lower()
    if explicit:
        return explicit == "password"
    label = f"{getattr(node, 'name', '') or ''} {getattr(node, 'text_hint', '') or ''}"
    return bool(_PASSWORD_LABEL_RE.search(label))


def detect_login(session: Any, nodes: list[Any]) -> LoginRequired | None:
    """Conservative login-wall detection shared by navigate and snapshot (§8.2).

    Triggers only on an HTTP 401 for the current main document, or when a visible,
    interactive password field dominates a page with few interactive nodes.
    Deliberately URL-agnostic (no path heuristics) to avoid ``/login-help``
    false positives. Returns the error the caller should raise, or ``None``.

    While ``session.login_pending`` is set and the wall persists, the returned
    error carries the waited/remaining seconds produced by
    ``session.login_required`` - that is the keepalive contract. Once the page
    no longer looks like a wall the pending flag is cleared.
    """
    url = getattr(getattr(session, "snapshot", None), "url", "") or ""
    if urlsplit(url).scheme == "chrome-error":
        # A failed navigation is neither login evidence nor proof the wall cleared.
        return None
    site = url_host(url) or url or "未知站点"
    status = getattr(session, "last_main_status", None)
    status_forced = status == 401
    interactive = [n for n in nodes if getattr(n, "interactive", False)]
    password = [
        n for n in nodes if _is_password_input(n) and getattr(n, "interactive", False) and getattr(n, "visible", False)
    ]
    form_dominant = bool(password) and len(interactive) <= _LOGIN_INTERACTIVE_LIMIT
    if status_forced or form_dominant:
        session.note_login_pending()
        reason = f"HTTP {status}" if status_forced else "登录表单主导（password 输入框）"
        return session.login_required(site=site, reason=reason)
    if getattr(session, "login_pending", False):
        session.clear_login_pending()
    return None
