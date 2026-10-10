# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local web cards consume bounded metadata and retain generic text fallback."""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from rich.style import Style
from rich.text import Text
from textual.css.query import NoMatches
from textual.widgets import Static

from chrys.app.tui.util.source_text import BIDI_AND_ZERO_WIDTH_CODEPOINTS
from chrys.app.tui.widgets.chat.tool_call import ToolCall
from chrys.foundation.i18n.formatting import sanitize_legacy_block, sanitize_legacy_scalar
from chrys.foundation.net.url import normalize_url
from chrys.foundation.tool_result_metadata import (
    WEB_FETCH_FINAL_URL_METADATA_KEY,
    WEB_SEARCH_METADATA_KEY,
    WEB_SEARCH_TITLE_MAX_CHARS,
)

# A search result's title is the label of its link: bidi controls could show it
# reversed or run it into the URL below, and zero-width characters hide text.
_BIDI_AND_ZERO_WIDTH = dict.fromkeys(BIDI_AND_ZERO_WIDTH_CODEPOINTS)


class WebToolCall(ToolCall):
    """Show clickable sources without parsing potentially truncated JSON text."""

    def set_complete(self, result: str, duration_ms: int = 0, **kwargs: Any) -> None:
        super().set_complete(result, duration_ms, **kwargs)
        if self.status != "complete" or self.metadata is None:
            return
        summary = Text()
        if self.tool_name == "web_search":
            payload = self.metadata.get(WEB_SEARCH_METADATA_KEY)
            if not isinstance(payload, dict):
                return
            provider, items = payload.get("provider"), payload.get("results")
            if not isinstance(provider, str) or not isinstance(items, list) or len(items) > 20:
                return
            summary.append(f"{sanitize_legacy_scalar(provider)} · {len(items)}\n")
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("title"), str):
                    return
                try:
                    url = normalize_url(item.get("url"))
                except ValueError:
                    return
                title = item["title"][:WEB_SEARCH_TITLE_MAX_CHARS].translate(_BIDI_AND_ZERO_WIDTH)
                summary.append(sanitize_legacy_scalar(title) + "\n", style=Style(link=url))
                summary.append(sanitize_legacy_scalar(url) + "\n", style="dim")
        else:
            value = self.metadata.get(WEB_FETCH_FINAL_URL_METADATA_KEY)
            if not isinstance(value, str):
                return
            try:
                url = normalize_url(value)
            except ValueError:
                return
            summary.append(sanitize_legacy_scalar(url) + "\n", style=Style(link=url))
            summary.append(Text(sanitize_legacy_block(self._truncate_result(result))))
        with suppress(NoMatches):
            self.query_one("#tc-body", Static).update(summary)
