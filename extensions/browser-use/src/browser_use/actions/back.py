# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_back`` - step back through the navigation history."""

from __future__ import annotations

import time
from typing import Any

from ..types import ToolBlock, text_block
from ._shared import (
    capture_and_register,
    detect_login,
    eval_value,
    render_snapshot,
    send_watching_dialog,
    stats_line,
)

NAME = "browser_back"

DESCRIPTION = (
    "Go back one entry in the page's navigation history (via Page.navigateToHistory-"
    "Entry, never history.back()). At the history start it returns a plain notice and "
    "does NOT error. On success it waits for the navigation and returns a fresh "
    "snapshot with new @eN refs. Not auto-retried (it advances history)."
)

PARAMS: dict[str, Any] = {}


async def _page_title(session: Any) -> str:
    value = await eval_value(session, "document.title")
    return "" if value is None else str(value)


async def follow_navigation(
    session: Any,
    method: str,
    command_params: dict[str, Any],
    *,
    cross_document: bool = False,
    expected_url: str | None = None,
) -> list[ToolBlock]:
    """Send a navigation *method*, wait for the load it starts, return a fresh snapshot.

    Shared by back/forward/reload: their responses carry no loaderId, so the session's
    own (current-page, main-frame) navigation record tells what happened: a commit
    with a loader is then waited to its load, a same-document navigation is done.
    *cross_document* (reload) accepts only a commit, never the old page's pushState;
    *expected_url* (a history step's destination) accepts only a same-document
    navigation that reached it.
    """
    client = session.require_client()
    nav_seq = session.nav_seq
    dialog_seq = session.dialog_seq
    # One budget for commit + load, so the action stays within the host's read timeout.
    deadline = time.monotonic() + session.cfg.nav_load_timeout

    async def send_and_wait() -> str | None:
        await client.send(
            method,
            command_params,
            session_id=session.session_id,
            timeout=session.cfg.nav_load_timeout,
        )
        loader = await session.wait_for_navigation(
            after=nav_seq,
            timeout=max(0.0, deadline - time.monotonic()),
            cross_document=cross_document,
            url=expected_url,
        )
        if loader:
            await session.wait_for_load(loader, timeout=max(0.1, deadline - time.monotonic()))
        return loader

    # A dialog pauses the page until it is answered - beforeunload ("Leave site?")
    # before the navigation, or the new page's own alert while it loads: report it
    # and release the action lock so browser_dialog can answer it.
    loader_id, dialog = await send_watching_dialog(session, dialog_seq, send_and_wait)
    if dialog is not None:
        # Same filter as the wait itself: the old page's own pushState is not this navigation.
        navigated = await session.wait_for_navigation(
            after=nav_seq, timeout=0, cross_document=cross_document, url=expected_url
        )
        if navigated is not None:
            state = f"已发送 {method}，导航已提交，新页面加载时弹出对话框"
            advice = "请调用 browser_dialog 处理后再 browser_snapshot（旧 ref 已失效）"
        else:
            state = f"已发送 {method}，页面弹出对话框，导航尚未发生"
            advice = "请调用 browser_dialog：accept 离开当前页并继续导航，dismiss 留在当前页；之后 browser_snapshot 复核"
        return [text_block("\n".join([state, dialog.fields(), "status=blocked retryable=false", advice]))]

    title = await _page_title(session)
    # The navigation event already bumped the epoch; do not bump again.
    nodes, _frame = await capture_and_register(session, url=session.snapshot.url, title=title, dom_changed=False)
    login_error = detect_login(session, nodes)
    if login_error is not None:
        raise login_error

    text = render_snapshot(session, nodes, url=session.snapshot.url, title=title)
    text = f"{text}\n{stats_line(session, nodes)}"
    if loader_id is None:
        text = (
            f"{text}\n未观察到导航（{session.cfg.nav_load_timeout:g}s 内主框架没有提交或同文档跳转）："
            "以上是当前页面；导航可能被取消、只改了子框架、被下载接管，或仍在进行（服务器很慢）——"
            "继续操作前请 browser_snapshot 复核，ref 可能随即失效"
        )
    session.note_success(session.target_id)
    return [text_block(text)]


async def step_history(session: Any, delta: int, edge_notice: str) -> list[ToolBlock]:
    """Move *delta* entries through the history; past either end return *edge_notice*."""
    client = session.require_client()
    history = await client.send(
        "Page.getNavigationHistory",
        session_id=session.session_id,
        timeout=session.cfg.cdp_timeout,
    )
    entries = history.get("entries") or []
    index = int(history.get("currentIndex") or 0) + delta
    if index < 0 or index >= len(entries):
        return [text_block(edge_notice)]
    entry = entries[index]
    return await follow_navigation(
        session,
        "Page.navigateToHistoryEntry",
        {"entryId": entry.get("id")},
        expected_url=str(entry.get("url") or "") or None,
    )


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    return await step_history(session, -1, "已经在历史起点，未回退")
