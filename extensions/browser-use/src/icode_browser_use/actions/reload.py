# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_reload`` - reload the current page."""

from __future__ import annotations

from typing import Any

from ..types import ToolBlock
from .back import follow_navigation
from .navigate import _precheck_url

NAME = "browser_reload"

DESCRIPTION = (
    "Reload the current page (Page.reload) and return a fresh snapshot with new @eN "
    "refs; every old ref becomes stale. ignore_cache=true bypasses the HTTP cache. A "
    "page that was the result of a form POST may submit it again, so this is not "
    "auto-retried; prefer browser_snapshot when you only need to re-read the page."
)

PARAMS: dict[str, Any] = {
    "ignore_cache": {"type": "boolean", "default": False},
}


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    current_url = str(getattr(getattr(session, "snapshot", None), "url", "") or "").strip()
    lowered = current_url.lower()
    if current_url and lowered not in {"about:blank", "about:srcdoc"} and not lowered.startswith("chrome-error://"):
        _precheck_url(session, current_url)
    return await follow_navigation(
        session, "Page.reload", {"ignoreCache": arguments["ignore_cache"]}, cross_document=True
    )
