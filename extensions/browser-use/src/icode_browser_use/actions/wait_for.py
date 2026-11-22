# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""``browser_wait_for`` - bounded, cancellable waiting on page conditions.

The wait is a deadline loop of cheap DOM probes, never a network-idle signal:
each poll reads one element's visibility (``getBoundingClientRect`` plus computed
style), one page value, or a mutation counter, so an animated or polling page
still settles the condition. Every CDP call is bounded by the remaining budget,
and a free-text target refreshes the fused snapshot at most every
``_REFRESH_MIN_SECONDS`` (inside the same budget), which is how an element that
appears later becomes visible to the ranking used by every other action. The
loop awaits between polls, so a cancelled tool call stops the wait immediately.

``stable`` installs its own ``MutationObserver`` per document (``window.__chrys_stable``;
the snapshot probe's ``__chrys_mutations`` is a plain counter and stays untouched)
or per target node and succeeds once no mutation was seen for the quiet window;
``text_changed`` hashes the text at the first poll and succeeds on the first
difference; ``count`` counts ``browser_find`` matches in a refreshed snapshot.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from ..errors import (
    BadRequest,
    BrowserError,
    CDPTimeout,
    ConnectionClosed,
    StaleRef,
    TargetAmbiguous,
)
from ..snapshot.serializer import sanitize_text
from ..types import ToolBlock, text_block
from ._shared import capture_and_register
from ._shared import capture_and_register as _shared_capture
from .extract import _DEEP_TEXT_JS
from .find import matching_nodes

NAME = "browser_wait_for"

DESCRIPTION = (
    "Wait until a condition holds: visible, hidden or editable for a target (an @eN ref "
    "or free text), text in the page including Shadow DOM (or in target when given), url "
    "containing value, text_changed (the target's or page's text differs from "
    "when the wait started; returns the new text), stable (no DOM mutation in "
    "the page or target for value ms, default 1500, max 5000; not network idle — "
    "always pass target on pages with video/ads; marks completion=inferred_stable), or count (the number of browser_find "
    "matches for target satisfies value such as '>3' or '=2'). Polls DOM state until "
    "timeout_ms (100-180000), then reports exactly what was last observed. Set return_mode="
    "'ref'|'snapshot'|'text' (bounded by max_chars) to deliver the matched ref, scoped snapshot, "
    "or text in the same call instead of following up with browser_find/browser_extract; on timeout "
    "with return_mode='text', partial observed text and progress are included. After sending "
    "a chat message prefer wait_for(hidden, 'Stop generating', return_mode='text') or "
    "wait_for(stable, target=<last message ref>, return_mode='text') over sleeping."
)

PARAMS: dict[str, Any] = {
    "condition": {
        "type": "string",
        "enum": ["visible", "hidden", "editable", "text", "url", "text_changed", "stable", "count"],
        "required": True,
    },
    "target": {"type": "string", "default": ""},
    "value": {"type": "string", "default": ""},
    "timeout_ms": {"type": "integer", "min": 100, "max": 180000, "default": 5000},
    "return_mode": {"type": "string", "enum": ["none", "ref", "snapshot", "text"], "default": "none"},
    "max_chars": {"type": "integer", "min": 200, "max": 7000, "default": 2000},
}

EXPECT_PARAMS: dict[str, Any] = {
    "expect_condition": {
        "type": "string",
        "enum": ["", "visible", "hidden", "editable", "text", "url", "text_changed", "stable", "count"],
        "default": "",
    },
    "expect_target": {"type": "string", "default": ""},
    "expect_value": {"type": "string", "default": ""},
    "expect_timeout_ms": {"type": "integer", "min": 100, "max": 60000, "default": 5000},
    "expect_return": {"type": "string", "enum": ["none", "ref", "snapshot", "text"], "default": "none"},
}

_POLL_SECONDS = 0.15
_REFRESH_MIN_SECONDS = 0.5
_REFRESH_FLOOR_SECONDS = 0.1
_STABLE_DEFAULT_MS = 1500
_STABLE_MIN_MS = 100
_STABLE_MAX_MS = 5000
_LONG_WAIT_MS = 60000
_CHANGED_TEXT_CHARS = 300
_sleep = asyncio.sleep

_NODE_PROBE_FN = (
    f"function(){{{_DEEP_TEXT_JS}"
    " if(!this || !this.isConnected) return {present:false, visible:false, editable:false, text:''};"
    " const rect=this.getBoundingClientRect();"
    " const style=getComputedStyle(this);"
    " const visible=rect.width>0 && rect.height>0 && style.visibility!=='hidden'"
    "   && style.visibility!=='collapse' && style.display!=='none' && style.opacity!=='0';"
    " const tag=(this.tagName||'').toLowerCase();"
    " const type=(this.type||'').toLowerCase();"
    " const disabled=this.disabled===true || this.getAttribute('aria-disabled')==='true';"
    " const readOnly=this.readOnly===true || this.getAttribute('aria-readonly')==='true';"
    " const textLike=this.isContentEditable===true || tag==='textarea'"
    "   || (tag==='input' && ['','text','search','email','url','tel','password','number'].includes(type));"
    " const editable=!disabled && !readOnly && textLike;"
    " return {present:true, visible, editable, disabled, readOnly, tag,"
    "   text:String(deepText(this)||'').slice(0,20000)};"
    "}"
)

_PAGE_TEXT_EXPR = (
    f"(()=>{{{_DEEP_TEXT_JS} const body=document.body; return body?String(deepText(body)||'').slice(0,20000):'';}})()"
)

_OBSERVE_OPTIONS = "{subtree:true, childList:true, characterData:true, attributes:true}"

_STABLE_PAGE_EXPR = (
    "(()=>{"
    " let m=window.__chrys_stable;"
    " if(!m){"
    "  const obs=new MutationObserver(()=>{m.n++; m.at=performance.now();});"
    "  m={n:0, at:performance.now(), obs}; window.__chrys_stable=m;"
    f"  obs.observe(document.documentElement, {_OBSERVE_OPTIONS});"
    " }"
    " return {present:true, n:m.n, quiet_ms:performance.now()-m.at};"
    "})()"
)

_STABLE_PAGE_CLEANUP_EXPR = (
    "(()=>{ const m=window.__chrys_stable; if(m && m.obs){ try{ m.obs.disconnect(); }catch(_){} }"
    " delete window.__chrys_stable; return true; })()"
)

_STABLE_NODE_FN = (
    "function(){"
    " if(!this || !this.isConnected) return {present:false};"
    " let map=window.__chrys_stable_by_node;"
    " if(!map){map=new WeakMap(); window.__chrys_stable_by_node=map;}"
    " let m=map.get(this);"
    " if(!m){"
    "  const obs=new MutationObserver(()=>{m.n++; m.at=performance.now();});"
    "  m={n:0, at:performance.now(), obs}; map.set(this, m);"
    f"  obs.observe(this, {_OBSERVE_OPTIONS});"
    f"  if(this.shadowRoot){{ obs.observe(this.shadowRoot, {_OBSERVE_OPTIONS}); }}"
    " }"
    " return {present:true, n:m.n, quiet_ms:performance.now()-m.at};"
    "}"
)

_STABLE_NODE_CLEANUP_FN = (
    "function(){"
    " const map=window.__chrys_stable_by_node;"
    " if(map){ const m=map.get(this); if(m && m.obs){ try{ m.obs.disconnect(); }catch(_){} } map.delete(this); }"
    " return true;"
    "}"
)


def _shadow_text_hit(nodes: list[Any], value: str) -> bool:
    """Match *value* only against visible nodes' text/name (never href-derived names or hidden nodes)."""
    needle = value.casefold()
    if not needle:
        return False
    for node in nodes:
        if not getattr(node, "visible", False):
            continue
        name = "" if getattr(node, "name_from_href", False) else str(getattr(node, "name", "") or "")
        hint = str(getattr(node, "text_hint", "") or "")
        if needle in name.casefold() or needle in hint.casefold():
            return True
    return False


_GONE_RE = re.compile(r"could not find (?:node|object)|no node with given id|node .*detached", re.IGNORECASE)
_COUNT_RE = re.compile(r"^\s*(>=|<=|==|=|>|<)?\s*(\d+)\s*$")
_COUNT_OPS = {
    "=": lambda n, k: n == k,
    "==": lambda n, k: n == k,
    ">": lambda n, k: n > k,
    ">=": lambda n, k: n >= k,
    "<": lambda n, k: n < k,
    "<=": lambda n, k: n <= k,
}


def _is_gone(error: BrowserError) -> bool:
    """Whether a CDP error means the node itself is gone (not a transient failure)."""
    return bool(_GONE_RE.search(str(error)))


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _probe_timeout(session: Any, deadline: float) -> float:
    return max(0.05, min(session.cfg.evaluate_timeout, _remaining(deadline)))


def _evidence(session: Any) -> str:
    return f"document_epoch={getattr(session, 'page_epoch', 0)}"


def _snapshot_matches_epoch(session: Any) -> bool:
    """Whether no registered node belongs to an older document (an empty snapshot is not stale)."""
    return all(node.epoch == session.page_epoch for node in session.snapshot.nodes)


def _quiet_window_ms(value: str) -> int:
    if not value.strip():
        return _STABLE_DEFAULT_MS
    try:
        quiet = int(value)
    except ValueError:
        raise BadRequest("stable 条件的 value 必须是静默毫秒数（默认 1500，最大 5000）") from None
    return max(_STABLE_MIN_MS, min(_STABLE_MAX_MS, quiet))


def _count_predicate(value: str) -> tuple[str, int]:
    match = _COUNT_RE.match(value)
    if match is None:
        raise BadRequest("count 条件的 value 形如 '>3'、'>=3'、'=2'、'<5'")
    return match.group(1) or "=", int(match.group(2))


async def _evaluate(session: Any, expression: str, deadline: float) -> Any:
    client = session.require_client()
    response = await client.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=session.session_id,
        timeout=_probe_timeout(session, deadline),
    )
    return response.get("result", {}).get("value")


async def _call_node(
    session: Any, object_id: str, cdp_session_id: str | None, function: str, deadline: float
) -> dict[str, Any]:
    client = session.require_client()
    response = await client.send(
        "Runtime.callFunctionOn",
        {"objectId": object_id, "functionDeclaration": function, "returnByValue": True},
        session_id=cdp_session_id or session.session_id,
        timeout=_probe_timeout(session, deadline),
    )
    value = response.get("result", {}).get("value")
    return value if isinstance(value, dict) else {}


async def _probe_node(session: Any, object_id: str, cdp_session_id: str | None, deadline: float) -> dict[str, Any]:
    return await _call_node(session, object_id, cdp_session_id, _NODE_PROBE_FN, deadline)


async def _resolve_node(session: Any, entry: Any, deadline: float) -> str | None:
    """Resolve the target node; ``None`` means it is gone (detached or replaced).

    A node-only resolution keeps the coarse text-name comparison of
    ``BrowserSession.resolve_object_id`` out of the wait (a target whose own text
    changes is still the same element) and is bounded by the remaining budget.
    """
    client = session.require_client()
    try:
        resolved = await client.send(
            "DOM.resolveNode",
            {"backendNodeId": entry.backend_node_id},
            session_id=entry.cdp_session_id or session.session_id,
            timeout=_probe_timeout(session, deadline),
        )
    except ConnectionClosed:
        raise
    except CDPTimeout:
        raise
    except BrowserError as exc:
        if _is_gone(exc):
            return None
        raise
    object_id = str((resolved.get("object") or {}).get("objectId", ""))
    return object_id or None


async def _refresh(session: Any, deadline: float) -> bool:
    """Refresh the fused snapshot within the remaining budget (False if it could not)."""
    remaining = _remaining(deadline)
    if remaining <= _REFRESH_FLOOR_SECONDS:
        return False
    try:
        await asyncio.wait_for(capture_and_register(session), remaining)
    except ConnectionClosed:
        raise
    except (TimeoutError, asyncio.TimeoutError):
        return False
    except CDPTimeout:
        raise
    except BrowserError:
        return False
    return True


def _resolve_editable_free_text(session: Any, spec: str) -> Any | None:
    """Resolve a free-text target among editable nodes only (for ``editable`` waits)."""
    from ..snapshot.scoring import rank, resolve_ambiguity

    editable_nodes = [node for node in session.snapshot.nodes if getattr(node, "editable", False)]
    if not editable_nodes:
        return None
    candidates = rank(editable_nodes, spec)
    if not candidates:
        return None
    best = resolve_ambiguity(candidates)
    ref_map = getattr(session, "_ref_map", None)
    if isinstance(ref_map, dict) and best.node.ref in ref_map:
        return ref_map[best.node.ref]
    return session.resolve_target(f"@e{best.node.ref}#{best.node.epoch}") if best.node.ref and best.node.epoch else None


async def _lookup(
    session: Any,
    spec: str,
    deadline: float,
    *,
    refresh_allowed: bool,
    editable_only: bool = False,
) -> tuple[Any, str, bool]:
    """Resolve a free-text target; entry ``None`` means it is not in the page.

    Returns ``(entry, object_id, refreshed)``. The snapshot is refreshed at most
    once per call, and only when the caller allows it.
    """
    refreshed = False
    for attempt in range(2):
        try:
            entry = _resolve_editable_free_text(session, spec) if editable_only else session.resolve_target(spec)
        except ConnectionClosed:
            raise
        except TargetAmbiguous:
            raise
        except BrowserError:
            entry = None
        if entry is not None:
            object_id = await _resolve_node(session, entry, deadline)
            can_refresh = capture_and_register is not _shared_capture or hasattr(session, "read_frame_metrics")
            if object_id or attempt or not refresh_allowed or not can_refresh:
                return entry, object_id, refreshed
            # Entry from snapshot is detached (re-rendered): refresh once before returning.
            refreshed = await _refresh(session, deadline)
            if not refreshed:
                return entry, "", refreshed
            continue
        if attempt or not refresh_allowed:
            break
        refreshed = await _refresh(session, deadline)
        if not refreshed:
            break
    return None, "", refreshed


def _subject(condition: str, spec: str, value: str) -> str:
    if condition == "url" or (condition in {"text", "stable", "text_changed"} and not spec):
        return f"value={value!r}" if value else "page"
    if condition in {"text", "count", "stable"} and value:
        return f"target={spec} value={value!r}"
    return f"target={spec}"


def _success(
    session: Any, condition: str, spec: str, value: str, started: float, basis: str, detail: str = ""
) -> list[ToolBlock]:
    elapsed = int((time.monotonic() - started) * 1000)
    session.note_success(session.target_id)  # waiting is activity; keep the idle watchdog honest
    lines = [
        f"已满足条件：{condition} {_subject(condition, spec, value)}（等待 {elapsed} ms）",
        f"basis={basis}",
    ]
    if detail:
        lines.append(detail)
    lines.append(f"status=succeeded\n{_evidence(session)}")
    return [text_block("\n".join(lines))]


def _changed_text(text: str) -> str:
    excerpt = sanitize_text(text[:_CHANGED_TEXT_CHARS]).replace("\n", " ")
    return f"text={excerpt!r}" + ("…" if len(text) > _CHANGED_TEXT_CHARS else "")


def _format_bounded_text(text: str, max_chars: int, *, label: str = "content") -> str:
    cleaned = sanitize_text(text).strip()
    total = len(cleaned)
    clipped = cleaned[:max_chars]
    suffix = f"\n…(truncated, {total} total chars)" if total > max_chars else ""
    return f"observed_chars={total}\n{label}:\n{clipped}{suffix}"


def _format_ref_entry(entry: Any) -> str:
    token = getattr(entry, "token", None)
    if not token:
        index = getattr(entry, "index", None)
        if index is None:
            index = getattr(entry, "ref", None)
        epoch = getattr(entry, "epoch", None)
        token = f"@e{index}#{epoch}" if index and epoch else ""
    fp = getattr(entry, "fingerprint", None)
    raw_name = (fp.get("name") if isinstance(fp, dict) else None) or getattr(entry, "name", "") or ""
    name = sanitize_text(str(raw_name)).replace("\n", " ")[:120]
    if token:
        return f"matched_ref={token} name={name!r}"
    return f"matched_ref=unavailable name={name!r}"


async def _collect_return_payload(
    session: Any,
    *,
    return_mode: str,
    max_chars: int,
    condition: str,
    target: str,
    entry: Any,
    object_id: str,
    observed_text: str | None,
) -> str:
    """Deliver ref, scoped snapshot, or bounded text alongside a settled wait."""
    if return_mode == "none":
        return ""
    tail_deadline = time.monotonic() + 2.0
    if return_mode == "ref":
        if entry is not None:
            return _format_ref_entry(entry)
        if target and condition == "count":
            matches = matching_nodes(list(session.snapshot.nodes), target)
            if matches:
                refs = " ".join(f"@e{n.ref}#{n.epoch}" for n in matches[:10] if getattr(n, "ref", None))
                return f"matched_refs={refs}" if refs else ""
        if target and condition != "hidden":
            try:
                resolved = session.resolve_target(target)
                return _format_ref_entry(resolved)
            except ConnectionClosed:
                raise
            except Exception:
                return ""
        return ""
    if return_mode == "snapshot":
        from . import snapshot as snapshot_action

        snap_target = target if (target and condition not in {"hidden", "count"}) else ""
        try:
            blocks = await snapshot_action.run(
                session,
                {"scope": "viewport", "interactive_only": False, "target": snap_target, "since": 0},
            )
            rendered = "\n".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
        except ConnectionClosed:
            raise
        except Exception:
            try:
                blocks = await snapshot_action.run(
                    session,
                    {"scope": "viewport", "interactive_only": False, "target": "", "since": 0},
                )
                rendered = "\n".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
            except ConnectionClosed:
                raise
            except Exception:
                return ""
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars] + f"\n…(snapshot truncated at {max_chars} chars)"
        return f"snapshot:\n{rendered}" if rendered else ""
    if return_mode == "text":
        text_val = observed_text
        if text_val is None:
            try:
                if target and object_id and condition != "hidden":
                    probe = await _probe_node(
                        session,
                        object_id,
                        entry.cdp_session_id if entry else None,
                        tail_deadline,
                    )
                    text_val = str(probe.get("text", ""))
                else:
                    text_val = str(await _evaluate(session, _PAGE_TEXT_EXPR, tail_deadline) or "")
            except ConnectionClosed:
                raise
            except Exception:
                text_val = ""
        return _format_bounded_text(text_val, max_chars, label="content")
    return ""


async def read_text_baseline(session: Any, target: str = "") -> str | None:
    """Capture a read-only text baseline before a write action for ``expect_condition='text_changed'``."""
    deadline = time.monotonic() + 1.5
    try:
        if not target:
            return str(await _evaluate(session, _PAGE_TEXT_EXPR, deadline) or "")
        entry = session.resolve_target(target)
        if entry is None:
            return None
        object_id = await _resolve_node(session, entry, deadline)
        if not object_id:
            return None
        probe = await _probe_node(session, object_id, entry.cdp_session_id, deadline)
        return str(probe.get("text", "")) if probe.get("present") else None
    except ConnectionClosed:
        raise
    except Exception:
        return None


def _validate_wait_args(condition: str, target: str, value: str, timeout_ms: int) -> tuple[int, str, int]:
    """Validate wait_for / expect_* arguments before any wait or write dispatch."""
    if condition in {"visible", "hidden", "editable", "count"} and not target:
        raise BadRequest(f"{condition} 条件需要 target（@eN 引用或自由文本）")
    if condition in {"text", "url", "count"} and not value:
        raise BadRequest(f"{condition} 条件需要 value")
    if condition == "url" and target:
        raise BadRequest("url 条件不接受 target，请只给 value")
    if condition == "count" and target.startswith("@"):
        raise BadRequest("count 条件的 target 是 browser_find 的查询文本，不是 @eN 引用")
    quiet_ms = _quiet_window_ms(value) if condition == "stable" else 0
    if condition == "stable" and timeout_ms < quiet_ms:
        raise BadRequest(f"stable 条件的 timeout_ms（{timeout_ms}）必须不小于静默窗口 value（{quiet_ms} ms）")
    count_op, count_value = _count_predicate(value) if condition == "count" else ("=", 0)
    return quiet_ms, count_op, count_value


async def prepare_expect_baseline(session: Any, arguments: dict[str, Any]) -> str | None:
    """Validate ``expect_*`` parameters and capture a pre-write baseline before dispatching a write."""
    condition = str(arguments.get("expect_condition") or "")
    if not condition:
        return None
    _validate_wait_args(
        condition,
        str(arguments.get("expect_target") or ""),
        str(arguments.get("expect_value") or ""),
        int(arguments.get("expect_timeout_ms") or 5000),
    )
    if condition != "text_changed":
        return None
    return await read_text_baseline(session, str(arguments.get("expect_target") or ""))


async def run_post_write_expect(
    session: Any,
    arguments: dict[str, Any],
    *,
    baseline: str | None,
    action_blocks: list[ToolBlock],
) -> list[ToolBlock]:
    """Attach bounded read-only verification after a single write action without ever re-dispatching it."""
    condition = str(arguments.get("expect_condition") or "")
    if not condition:
        return action_blocks
    action_text = "\n".join(str(b.get("text", "")) for b in action_blocks if b.get("type") == "text")
    if any(marker in action_text for marker in ("status=blocked", "status=failed", "status=uncertain")):
        return [text_block(f"{action_text}\nexpect_status=skipped reason=write_not_confirmed")]
    expect_args = {
        "condition": condition,
        "target": str(arguments.get("expect_target") or ""),
        "value": str(arguments.get("expect_value") or ""),
        "timeout_ms": int(arguments.get("expect_timeout_ms") or 5000),
        "return_mode": str(arguments.get("expect_return") or "none"),
        "max_chars": 2000,
    }
    try:
        matched, wait_blocks = await _run_with_baseline_outcome(session, expect_args, baseline=baseline)
    except ConnectionClosed:
        raise
    except BrowserError as exc:
        return [text_block(f"{action_text}\nexpect_status=error expect_error={exc.code}: {exc}")]
    wait_text = "\n".join(str(b.get("text", "")) for b in wait_blocks if b.get("type") == "text")
    expect_status = "matched" if matched else "timeout"
    return [text_block(f"{action_text}\n[EXPECT]\nexpect_status={expect_status}\n{wait_text}")]


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    return await run_with_baseline(session, arguments, baseline=None)


async def run_with_baseline(
    session: Any,
    arguments: dict[str, Any],
    *,
    baseline: str | None = None,
) -> list[ToolBlock]:
    _matched, blocks = await _run_with_baseline_outcome(session, arguments, baseline=baseline)
    return blocks


async def _run_with_baseline_outcome(
    session: Any,
    arguments: dict[str, Any],
    *,
    baseline: str | None = None,
) -> tuple[bool, list[ToolBlock]]:
    condition = arguments["condition"]
    target = arguments["target"]
    value = arguments["value"]
    timeout_ms = arguments["timeout_ms"]
    return_mode = str(arguments.get("return_mode") or "none")
    max_chars = int(arguments.get("max_chars") or 2000)
    quiet_ms, count_op, count_value = _validate_wait_args(condition, target, value, timeout_ms)

    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    ref_spec = target.startswith("@")
    entry: Any = None
    object_id = ""
    if ref_spec:
        entry = session.resolve_target(target)
    refresh_at = started - _REFRESH_MIN_SECONDS

    last = "尚未观测"
    detail = ""
    observed_text: str | None = None
    count_epoch: int | None = None
    refresh_failed = False
    try:
        while True:
            ok = False
            basis = ""
            try:
                if condition == "url":
                    href = str(await _evaluate(session, "location.href", deadline) or "")
                    ok = value in href
                    basis = "url_match"
                    last = f"url={href[:300]}"
                elif condition == "text" and not target:
                    body = str(await _evaluate(session, _PAGE_TEXT_EXPR, deadline) or "")
                    observed_text = body
                    ok = value in body
                    basis = "text_match"
                    last = f"page_text_hit={ok}"
                    if not ok:
                        can_refresh = capture_and_register is not _shared_capture or hasattr(
                            session, "read_frame_metrics"
                        )
                        if (
                            can_refresh
                            and time.monotonic() - refresh_at >= _REFRESH_MIN_SECONDS
                            and _remaining(deadline) > _REFRESH_FLOOR_SECONDS
                        ):
                            if await _refresh(session, deadline):
                                refresh_at = time.monotonic()
                                if _shadow_text_hit(list(session.snapshot.nodes), value):
                                    ok = True
                                    basis = "shadow_text_match"
                                    last = "page_text_hit=True (shadow_snapshot)"
                        elif (
                            not can_refresh
                            and not getattr(session, "snapshot_dirty", False)
                            and _snapshot_matches_epoch(session)
                            and _shadow_text_hit(list(session.snapshot.nodes), value)
                        ):
                            ok = True
                            basis = "shadow_text_match"
                            last = "page_text_hit=True (shadow_snapshot)"
                elif condition == "text_changed" and not target:
                    body = str(await _evaluate(session, _PAGE_TEXT_EXPR, deadline) or "")
                    observed_text = body
                    if baseline is None:
                        baseline = body
                    ok = body != baseline
                    basis = "text_changed"
                    last = f"page_text_changed={ok}"
                    if ok:
                        detail = _changed_text(body)
                elif condition == "stable" and not target:
                    probe = await _evaluate(session, _STABLE_PAGE_EXPR, deadline)
                    probe = probe if isinstance(probe, dict) else {}
                    quiet = float(probe.get("quiet_ms") or 0.0)
                    ok = quiet >= quiet_ms
                    basis = "mutations_quiet"
                    last = f"page quiet_ms={int(quiet)} mutations={probe.get('n', '?')}"
                    if ok:
                        detail = f"{last} completion=inferred_stable"
                elif condition == "count":
                    due = time.monotonic() - refresh_at >= _REFRESH_MIN_SECONDS
                    can_capture = _remaining(deadline) > _REFRESH_FLOOR_SECONDS
                    if due and can_capture and await _refresh(session, deadline):
                        refresh_at = time.monotonic()
                        count_epoch = session.page_epoch
                    elif due and can_capture:
                        refresh_failed = True
                    if count_epoch != session.page_epoch or not _snapshot_matches_epoch(session):
                        if refresh_failed:
                            last = "count snapshot refresh failed; retrying"
                        elif not can_capture:
                            last = "剩余预算不足以刷新快照（<0.1s），请加大 timeout_ms"
                        else:
                            last = "count snapshot is stale; retrying"
                    else:
                        found = len(matching_nodes(list(session.snapshot.nodes), target))
                        ok = _COUNT_OPS[count_op](found, count_value)
                        basis = "find_count"
                        last = f"count={found}"
                else:
                    if not ref_spec:
                        refresh_allowed = time.monotonic() - refresh_at >= _REFRESH_MIN_SECONDS
                        entry, object_id, refreshed = await _lookup(
                            session,
                            target,
                            deadline,
                            refresh_allowed=refresh_allowed,
                            editable_only=(condition == "editable"),
                        )
                        if refreshed:
                            refresh_at = time.monotonic()
                    else:
                        if not object_id:
                            object_id = await _resolve_node(session, entry, deadline) or ""
                    if entry is None:
                        last = f"target={target} not_found"
                        if condition == "hidden" and _snapshot_matches_epoch(session):
                            ok, basis = True, "not_found"
                            detail = "目标不在当前快照（不存在按已隐藏处理）；若是笔误请核对目标名"
                        elif condition == "hidden":
                            last += "（快照来自旧文档，正在刷新）"
                    elif not object_id:
                        last = f"target={target} gone"
                        if condition == "hidden":
                            ok, basis = True, "gone"
                        elif ref_spec:
                            raise StaleRef("引用已失效（元素已被替换），请重新 snapshot")
                        else:
                            # A free-text target re-rendered since the snapshot: look again.
                            entry, object_id = None, ""
                    else:
                        function = _STABLE_NODE_FN if condition == "stable" else _NODE_PROBE_FN
                        try:
                            probe = await _call_node(
                                session, object_id, entry.cdp_session_id if entry else None, function, deadline
                            )
                        except ConnectionClosed:
                            raise
                        except CDPTimeout:
                            raise
                        except BrowserError as exc:
                            if not _is_gone(exc):
                                raise
                            probe = {}
                        if not probe.get("present"):
                            last = f"target={target} gone"
                            if condition == "hidden":
                                ok, basis = True, "gone"
                            elif ref_spec:
                                raise StaleRef("引用已失效（元素已被替换），请重新 snapshot")
                            else:
                                entry, object_id = None, ""
                        elif condition == "stable":
                            quiet = float(probe.get("quiet_ms") or 0.0)
                            ok = quiet >= quiet_ms
                            basis = "mutations_quiet"
                            last = f"target={target} quiet_ms={int(quiet)} mutations={probe.get('n', '?')}"
                            if ok:
                                detail = f"{last} completion=inferred_stable"
                        elif condition == "text_changed":
                            text = str(probe.get("text", ""))
                            observed_text = text
                            if baseline is None:
                                baseline = text
                            ok = text != baseline
                            basis = "text_changed"
                            last = f"target={target} text_changed={ok}"
                            if ok:
                                detail = _changed_text(text)
                        else:
                            visible = bool(probe.get("visible"))
                            if "text" in probe:
                                observed_text = str(probe.get("text", ""))
                            last = f"target={target} visible={visible}"
                            if condition == "visible":
                                ok, basis = visible, "visible"
                                if not visible:
                                    last = f"target={target} visible=False state=found_hidden"
                            elif condition == "editable":
                                editable = bool(probe.get("editable"))
                                ok, basis = (visible and editable), "editable"
                                if not visible:
                                    last = f"target={target} visible=False state=found_hidden"
                                elif not editable:
                                    reason_bits = []
                                    if probe.get("disabled"):
                                        reason_bits.append("disabled")
                                    if probe.get("readOnly"):
                                        reason_bits.append("readonly")
                                    if probe.get("tag"):
                                        reason_bits.append(f"tag={probe['tag']}")
                                    suffix = f" ({' '.join(reason_bits)})" if reason_bits else ""
                                    last = f"target={target} state=found_not_editable visible=True{suffix}"
                            elif condition == "hidden":
                                ok, basis = (not visible), "invisible"
                            else:
                                ok = value in str(probe.get("text", ""))
                                basis = "text_match"
                                last += f" text_hit={ok}"
            except ConnectionClosed:
                raise
            except StaleRef:
                raise
            except TargetAmbiguous:
                raise
            except (CDPTimeout, BrowserError) as exc:
                last = f"probe_failed:{type(exc).__name__}"
                ok = False

            if ok:
                payload = await _collect_return_payload(
                    session,
                    return_mode=return_mode,
                    max_chars=max_chars,
                    condition=condition,
                    target=target,
                    entry=entry,
                    object_id=object_id,
                    observed_text=observed_text,
                )
                if payload:
                    detail = f"{detail}\n{payload}" if detail else payload
                return True, _success(session, condition, target, value, started, basis, detail)
            remaining = _remaining(deadline)
            if remaining <= 0:
                session.note_success(session.target_id)  # the wait itself was activity
                lines = [
                    f"等待超时：{condition} {_subject(condition, target, value)} 在 {timeout_ms} ms 内未满足",
                    f"last={last}",
                ]
                if return_mode == "text":
                    partial = await _collect_return_payload(
                        session,
                        return_mode="text",
                        max_chars=max_chars,
                        condition=condition,
                        target=target,
                        entry=entry,
                        object_id=object_id,
                        observed_text=observed_text,
                    )
                    if partial:
                        lines.append(partial.replace("\ncontent:\n", "\npartial_text:\n", 1))
                elif observed_text:
                    lines.append(_format_bounded_text(observed_text, min(max_chars, 800), label="partial_text"))
                if timeout_ms > _LONG_WAIT_MS:
                    lines.append(
                        f"elapsed_ms={int((time.monotonic() - started) * 1000)}（长等待已用尽，请改变条件或先 snapshot）"
                    )
                lines.append("下一步：browser_snapshot 或 browser_extract 核对页面，再换条件/目标等待；不要 sleep 轮询")
                lines.append(f"status=timeout retryable=true\n{_evidence(session)}")
                return False, [text_block("\n".join(lines))]
            await _sleep(min(_POLL_SECONDS, remaining))
    finally:
        if condition == "stable":
            cleanup_deadline = time.monotonic() + 1.0
            try:
                if not target:
                    await _evaluate(session, _STABLE_PAGE_CLEANUP_EXPR, cleanup_deadline)
                elif object_id:
                    await _call_node(
                        session,
                        object_id,
                        entry.cdp_session_id if entry else None,
                        _STABLE_NODE_CLEANUP_FN,
                        cleanup_deadline,
                    )
            except Exception:
                pass
