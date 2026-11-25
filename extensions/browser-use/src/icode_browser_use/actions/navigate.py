# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_navigate`` - open a URL and return a fresh compact snapshot."""

from __future__ import annotations

from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit

from ..errors import BadRequest, Blocked, Unavailable
from ..types import ToolBlock, text_block
from ._shared import (
    capture_and_register,
    detect_login,
    eval_value,
    login_hint,
    render_snapshot,
    send_watching_dialog,
    stats_line,
)

NAME = "browser_navigate"

DESCRIPTION = (
    "Open an absolute HTTP(S) URL or a path relative to the current page. Public "
    "sites work by default; a configured allowlist restricts access, and local "
    "origins require explicit access. Waits for the load (or a soft navigation), then returns a "
    "fresh compact snapshot with new @eN refs. NOT auto-retried: a navigate can "
    "have side effects, so retry only after checking the result."
)

PARAMS: dict[str, Any] = {"url": {"type": "string", "required": True}}


def _split_url(url: str) -> SplitResult:
    # Chrome treats backslashes as separators; urllib does not.
    if "\\" in url or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise BadRequest("URL 含有不支持的字符")
    try:
        parts = urlsplit(url)
        _ = parts.port
        return parts
    except ValueError as exc:
        raise BadRequest("URL 格式无效") from exc


def _precheck_url(session: Any, url: str) -> str:
    """Resolve and check the final URL before Chrome is asked to navigate."""
    from ..policy import canonical_origin, scheme_allowed, url_host_allowed

    text = (url or "").strip()
    if not text:
        raise BadRequest("url 不能为空")
    parts = _split_url(text)
    if parts.scheme and not scheme_allowed(parts.scheme):
        raise Blocked(f"不支持的 scheme：{parts.scheme}（仅允许 http/https）")
    if not parts.scheme:
        base = session.snapshot.url or ""
        base_parts = _split_url(base)
        if not scheme_allowed(base_parts.scheme) or not base_parts.hostname:
            raise BadRequest("相对 URL 需要有效的 http/https 当前页面，请提供绝对 URL")
        text = urljoin(base, text)
        parts = _split_url(text)
    if not scheme_allowed(parts.scheme):
        raise Blocked(f"不支持的 scheme：{parts.scheme}（仅允许 http/https）")
    host = parts.hostname or ""
    if not host:
        raise BadRequest("URL 必须包含有效的主机名")
    domains = getattr(session.cfg, "allowed_domains", ())
    origins = getattr(session.cfg, "allowed_origins", ())
    if not url_host_allowed(text, domains, origins):
        reason = "目标不在域名白名单内" if domains or origins else "目标不属于公网地址"
        raise Blocked(f"{reason}：{host}", blocked_origin=canonical_origin(text))
    return text


async def _page_title(session: Any) -> str:
    value = await eval_value(session, "document.title")
    return "" if value is None else str(value)


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    from ..policy import canonical_origin

    url = _precheck_url(session, arguments["url"])

    client = session.require_client()
    nav_seq = session.nav_seq
    dialog_seq = getattr(session, "dialog_seq", 0)
    policy = getattr(session, "_policy", None)
    blocked_before = policy.blocked if policy is not None else 0
    if policy is not None:
        policy.last_blocked_document_origin = ""
        policy.last_blocked_origin = ""

    async def _send_nav() -> dict[str, Any]:
        return await client.send(
            "Page.navigate",
            {"url": url},
            session_id=session.session_id,
            timeout=session.cfg.nav_load_timeout,
        )

    if callable(getattr(session, "dialog_since", None)):
        response, dialog = await send_watching_dialog(session, dialog_seq, _send_nav)
        if dialog is not None:
            return [
                text_block(
                    "\n".join(
                        [
                            "已发送 Page.navigate，页面弹出对话框，导航尚未完成",
                            dialog.fields(),
                            "status=blocked retryable=false",
                            "请先调用 browser_dialog（accept 离开当前页 / dismiss 留在此页），再 browser_snapshot 复核",
                        ]
                    )
                )
            ]
    else:
        response = await _send_nav()
    response = response or {}
    error_text = str(response.get("errorText") or "")
    if error_text:
        if "blocked" in error_text.lower():
            blocked_origin = ""
            if policy is not None and policy.blocked > blocked_before:
                blocked_origin = policy.last_blocked_document_origin or policy.last_blocked_origin
            if not blocked_origin:
                blocked_origin = canonical_origin(url)
            detail = f"{error_text} ({blocked_origin})" if blocked_origin else error_text
            raise Blocked(detail, blocked_origin=blocked_origin)
        raise Unavailable(error_text)

    loader_id = str(response.get("loaderId") or "")
    if loader_id:
        await session.wait_for_load(loader_id, timeout=session.cfg.nav_load_timeout)
    else:
        # Same-document (soft) navigation: no new loader id is issued.
        await session.wait_for_navigation(after=nav_seq, timeout=session.cfg.nav_load_timeout)

    title = await _page_title(session)
    # A navigation is a new generation: ``Page.frameNavigated`` already bumped the
    # epoch, so we must NOT ask ``register_snapshot`` to bump again (dom_changed=False).
    nodes, _frame = await capture_and_register(session, url=session.snapshot.url or url, title=title, dom_changed=False)
    login_error = detect_login(session, nodes)
    if login_error is not None:
        raise login_error

    page_url = session.snapshot.url or url
    text = render_snapshot(session, nodes, url=page_url, title=title)
    text = f"{text}\n{stats_line(session, nodes)}"
    hint = login_hint(page_url)
    if hint:
        text = f"{text}\n{hint}"
    session.note_success(session.target_id)
    return [text_block(text)]
