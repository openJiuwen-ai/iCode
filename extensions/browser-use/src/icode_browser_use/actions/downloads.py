# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_downloads`` - list the downloads this handle tracked."""

from __future__ import annotations

import json
from typing import Any

from ..types import ToolBlock, text_block

NAME = "browser_downloads"

DESCRIPTION = (
    "List the downloads this handle tracked (newest last): guid, state "
    "(in_progress/completed/canceled/failed), safe file name, received bytes, "
    "absolute path and reason. Controlled downloads need ICODE_BROWSER_DOWNLOADS=allow "
    "and the Owned backend; Attach/Bridge/Broker keep downloads denied and never "
    "redirect the user's own downloads. A click that starts a download returns before "
    "it finishes: poll this tool to see the result. Files are saved under the session's "
    "downloads folder with a sanitised, never-overwritten name; transfers above "
    "ICODE_BROWSER_DOWNLOAD_MAX_MB are canceled."
)

PARAMS: dict[str, Any] = {}


def _row(info: Any) -> str:
    total = f"/{info.total_bytes}" if info.total_bytes is not None else ""
    parts = [
        f"guid={info.guid}",
        f"state={info.state}",
        f"name={json.dumps(info.name, ensure_ascii=False)}",
        f"bytes={info.bytes_received}{total}",
    ]
    if info.path is not None:
        parts.append(f"path={json.dumps(str(info.path), ensure_ascii=False)}")
    if info.url:
        parts.append(f"url={json.dumps(info.url, ensure_ascii=False)}")
    if info.reason:
        parts.append(f"reason={json.dumps(info.reason, ensure_ascii=False)}")
    return "- " + " ".join(parts)


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    directory = session.download_directory
    downloads = session.list_downloads()
    lines: list[str] = []
    if directory is None:
        lines.append("下载未启用：受控下载需要 ICODE_BROWSER_DOWNLOADS=allow，且仅 Owned 后端支持")
        lines.append("Attach/Bridge/Broker 不启用受控下载，也不会改动用户自己的下载")
    else:
        limit_mb = session.cfg.download_max_bytes / (1024 * 1024)
        lines.append(f"下载 {len(downloads)} 个（目录 {directory}；上限 {limit_mb:g} MB）")
    lines.extend(_row(info) for info in downloads)
    if directory is not None and not downloads:
        lines.append("还没有下载；点击链接触发下载后，用本工具查看完成状态、文件名、大小与路径")
    if directory is not None:
        lines.append("文件使用绝对路径读取；文件名已规范化且不会覆盖已有文件")
    return [text_block("\n".join(lines))]
