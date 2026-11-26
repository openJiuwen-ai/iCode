# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The execution contract shared by every front end.

validate -> action lock -> budget -> single-flight start -> dispatch -> timeout
recycling happens once here. The extension adapter only translates errors to
``ToolError``; the Broker serves the same contract over its local socket.
"""

from __future__ import annotations

import contextlib
from typing import Any

from .actions import dispatch
from .errors import PRE_DISPATCH_ERROR_CODES, BrowserError, CDPTimeout, PageHung
from .types import ToolBlock, ToolDef
from .validation import validate

_OBSERVE_TOOLS = frozenset(
    {
        "browser_snapshot",
        "browser_find",
        "browser_extract",
        "browser_screenshot",
        "browser_tabs",
    }
)

_WRITE_TOOLS = frozenset(
    {
        "browser_click",
        "browser_drag",
        "browser_fill",
        "browser_press_key",
        "browser_upload",
        "browser_select_option",
        "browser_dialog",
        "browser_evaluate",
    }
)


class BrowserRuntime:
    """Plugin-side execution boundary over one browser session."""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def execute(
        self,
        tool: ToolDef,
        arguments: Any,
        *,
        runner: Any = None,
    ) -> list[ToolBlock]:
        call = runner or call_tool
        return await call(self.session, tool, arguments)


async def call_tool(session: Any, tool: ToolDef, arguments: Any) -> list[ToolBlock]:
    """Validate *arguments* and run one tool call against *session*.

    Raises a :class:`~icode_browser_use.errors.BrowserError` for every failure
    the model should read; a timeout that crosses the recycle threshold is
    turned into :class:`PageHung` after the page is recycled.
    """
    args = validate(tool.params, arguments)
    is_observe = tool.name in _OBSERVE_TOOLS
    budget_checked = False
    async with session.action_lock(tool.name):
        try:
            if tool.name in {"browser_download_cancel", "browser_tab_close"}:
                # Cancelling a tracked transfer or closing an owned tab never needs a
                # browser start: an idle handle has no download and no tab to act on.
                session.note_action()
                budget_checked = True
            elif tool.name not in {
                "browser_close",
                "browser_action_status",
                "browser_downloads",
                "browser_logs",
                "browser_network_body",
            }:
                note_observe = getattr(session, "note_observe_action", None)
                if is_observe and callable(note_observe):
                    note_observe()
                else:
                    session.note_action()
                budget_checked = True
                await session.ensure_started()
        except BrowserError as exc:
            # Any error raised before dispatch (budget exhausted, missing Chrome,
            # launch failure, lock busy, superseded slot) never dispatched a write.
            exc.pre_dispatch = True
            raise
        before_journal = getattr(session, "_journal", None)
        before_dispatched = getattr(before_journal, "dispatched_count", 0) if before_journal is not None else 0
        try:
            blocks = await dispatch(session, tool.name, args)
        except CDPTimeout as exc:
            if tool.name in _WRITE_TOOLS:
                after_journal = getattr(session, "_journal", None)
                after_dispatched = getattr(after_journal, "dispatched_count", 0) if after_journal is not None else 0
                if after_dispatched == before_dispatched:
                    note = getattr(session, "note_unrecorded_dispatched_write", None)
                    if callable(note):
                        note()
            target = exc.target_id or session.target_id
            if session.note_timeout_and_should_recycle(target):
                with contextlib.suppress(Exception):
                    await session.recycle_page()
                raise PageHung("页面无响应，已回收该标签页，请重新 browser_navigate") from exc
            raise
        except BrowserError as exc:
            if tool.name in _WRITE_TOOLS and exc.code not in PRE_DISPATCH_ERROR_CODES and not exc.pre_dispatch:
                after_journal = getattr(session, "_journal", None)
                after_dispatched = getattr(after_journal, "dispatched_count", 0) if after_journal is not None else 0
                if after_dispatched == before_dispatched:
                    note = getattr(session, "note_unrecorded_dispatched_write", None)
                    if callable(note):
                        note()
            raise
        if budget_checked and blocks:
            warn_fn = getattr(session, "action_budget_warning", None)
            if callable(warn_fn):
                warning = warn_fn(observe=is_observe)
                if warning:
                    for block in reversed(blocks):
                        if isinstance(block, dict) and block.get("type") == "text":
                            existing = str(block.get("text") or "")
                            block["text"] = f"{existing}\n{warning}" if existing else warning
                            break
        return blocks
