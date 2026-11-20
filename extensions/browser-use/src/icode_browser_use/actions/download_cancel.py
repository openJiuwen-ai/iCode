# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_download_cancel`` - cancel one in-progress tracked download."""

from __future__ import annotations

from typing import Any

from ..errors import BadRequest
from ..types import ToolBlock, text_block
from .actionability import evidence_line, mark_dispatched, new_action_id, prepare_action, record_action

NAME = "browser_download_cancel"

DESCRIPTION = (
    "Cancel an in-progress download by its guid from browser_downloads. Chrome stops "
    "the transfer and the partial file is removed from the session's downloads folder; "
    "completed, canceled or failed downloads are reported as they are and nothing is "
    "sent. Controlled downloads need ICODE_BROWSER_DOWNLOADS=allow and the Owned backend."
)

PARAMS: dict[str, Any] = {
    "guid": {"type": "string", "required": True},
}


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    guid = str(arguments["guid"])
    action_id = new_action_id()
    if session.download_directory is None:
        text = "\n".join(
            [
                "下载未启用：受控下载需要 ICODE_BROWSER_DOWNLOADS=allow，且仅 Owned 后端支持",
                "status=blocked retryable=false",
                evidence_line(session, action_id),
            ]
        )
        record_action(session, action_id, NAME, "blocked", text)
        return [text_block(text)]

    info = next((item for item in session.list_downloads() if item.guid == guid), None)
    if info is None:
        text = "\n".join([f"未知下载：{guid}（请先 browser_downloads）", "status=blocked retryable=false"])
        record_action(session, action_id, NAME, "blocked", text)
        return [text_block(text)]
    if info.state != "in_progress":
        text = "\n".join(
            [
                f"未取消下载 {info.name}：当前状态 {info.state}（只有 in_progress 的下载可以取消）",
                "status=blocked retryable=false",
                evidence_line(session, action_id),
            ]
        )
        record_action(session, action_id, NAME, "blocked", text)
        return [text_block(text)]

    prepare_action(session, action_id, NAME, f"prepared cancel of download {guid}")
    try:
        info = await session.cancel_download(guid)
    except BadRequest as exc:
        text = "\n".join([exc.message, "status=uncertain retryable=false", evidence_line(session, action_id)])
        record_action(session, action_id, NAME, "uncertain", text)
        return [text_block(text)]
    if info.state != "canceled":
        text = "\n".join(
            [
                f"未取消下载 {info.name}：完成于取消之前（状态 {info.state}）",
                "status=blocked retryable=false",
                evidence_line(session, action_id),
            ]
        )
        record_action(session, action_id, NAME, "blocked", text)
        return [text_block(text)]
    mark_dispatched(session, action_id, NAME, f"dispatched cancel of download {guid}")
    text = "\n".join(
        [
            f"已取消下载 {info.name}",
            f"bytes={info.bytes_received}",
            "已删除未完成的文件",
            "status=succeeded retryable=false",
            evidence_line(session, action_id),
        ]
    )
    record_action(session, action_id, NAME, "succeeded", text)
    return [text_block(text)]
