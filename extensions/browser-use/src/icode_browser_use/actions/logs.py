# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_logs`` - read the current tab's console and network log."""

from __future__ import annotations

import json
from typing import Any

from ..page_log import MAX_ENTRIES, scrub
from ..types import ToolBlock, text_block

NAME = "browser_logs"

DESCRIPTION = (
    "Read the current tab's recent console messages (console.* calls and uncaught "
    "exceptions) and network outcomes (XHR/fetch responses, HTTP >= 400, failed or "
    "blocked requests), oldest first, each with a #seq number. Use it to diagnose a "
    "page that does not react as expected. Pass since=<cursor> from the previous "
    "result to read only newer entries; errors_only=true drops info lines. Messages "
    "are redacted and truncated; URLs show origin+path only. Collection starts when "
    "the browser starts and keeps the newest entries per handle; a popup tab's console "
    "is only collected from its first browser_tab_switch, and a cross-process iframe's "
    "from the first snapshot that reads it."
)

PARAMS: dict[str, Any] = {
    "kind": {"enum": ["all", "console", "network"], "default": "all"},
    "errors_only": {"type": "boolean", "default": False},
    "since": {"type": "integer", "default": 0, "min": 0, "description": "Only entries after this #seq"},
    "limit": {"type": "integer", "default": 30, "min": 1, "max": 100},
}


def _line(entry: Any) -> str:
    text = json.dumps(scrub(entry.text), ensure_ascii=False)
    url = f" {scrub(entry.url)}" if entry.url else ""
    return f"#{entry.seq} {entry.kind}.{entry.level}{url} {text}"


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    log = session.page_log
    if log.disabled_reason:
        return [text_block(f"没有日志：{log.disabled_reason}")]
    since = arguments["since"]
    stale_cursor = since > log.seq  # the log was rebuilt (e.g. a new Broker session) after that cursor
    entries = log.entries(
        session.target_id,
        since=0 if stale_cursor else since,
        kind=arguments["kind"],
        errors_only=arguments["errors_only"],
    )
    shown = entries[-arguments["limit"] :]
    lines = [f"日志 {len(shown)} 条（当前标签页；cursor {log.seq}，下次传 since={log.seq} 只看新日志）"]
    if stale_cursor:
        lines.append(f"since={since} 大于当前 cursor {log.seq}（日志已重建），已从头读取")
    elif since + 1 < log.oldest_seq:
        lines.append(
            f"since={since} 之后有日志已不在缓冲区（各标签页共享 {MAX_ENTRIES} 条被新日志挤出，"
            f"或浏览器重启后已清空；最早保留 #{log.oldest_seq}）"
        )
    if len(entries) > len(shown):
        lines.append(f"… 更早的 {len(entries) - len(shown)} 条未显示（调大 limit，或用 errors_only/kind 过滤）")
    lines.extend(_line(entry) for entry in shown)
    if not entries:
        lines.append(f"没有匹配的日志（每个句柄只保留最近 {MAX_ENTRIES} 条；浏览器启动后才开始收集）")
    return [text_block("\n".join(lines))]
