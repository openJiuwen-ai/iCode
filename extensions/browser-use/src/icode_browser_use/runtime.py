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
from .errors import CDPTimeout, PageHung
from .types import ToolBlock, ToolDef
from .validation import validate


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
    async with session.action_lock(tool.name):
        if tool.name in {"browser_download_cancel", "browser_tab_close"}:
            # Cancelling a tracked transfer or closing an owned tab never needs a
            # browser start: an idle handle has no download and no tab to act on.
            session.note_action()
        elif tool.name not in {"browser_close", "browser_action_status", "browser_downloads", "browser_logs"}:
            session.note_action()
            await session.ensure_started()
        try:
            return await dispatch(session, tool.name, args)
        except CDPTimeout as exc:
            target = exc.target_id or session.target_id
            if session.note_timeout_and_should_recycle(target):
                with contextlib.suppress(Exception):
                    await session.recycle_page()
                raise PageHung("页面无响应，已回收该标签页，请重新 browser_navigate") from exc
            raise
