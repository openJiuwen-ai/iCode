# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``browser_upload`` - attach one local file to a file input (DOM.setFileInputFiles)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import BadRequest, BrowserError, CDPTimeout, ConnectionClosed
from ..types import ToolBlock, text_block
from ._shared import call_value, resolve_target, send_watching_dialog
from .actionability import evidence_line, mark_dispatched, new_action_id, prepare_action, record_action

NAME = "browser_upload"

DESCRIPTION = (
    "Set a local file on an <input type=file> element. target is an @eN ref or "
    "free text; path must be an existing regular file under the configured upload "
    "root. The tool only uses DOM.setFileInputFiles; it never clicks a file picker."
)

PARAMS: dict[str, Any] = {
    "target": {"type": "string", "required": True},
    "path": {"type": "string", "required": True},
}

_IS_FILE_INPUT_FN = (
    "function(){"
    " const disabled=this.disabled===true || (typeof this.matches==='function' && this.matches(':disabled'));"
    " return {ok:(this.tagName||'').toLowerCase()==='input' && this.type==='file' && !disabled};"
    "}"
)
_FILE_COUNT_FN = "function(){ return {ok:this.type==='file', count:this.files?this.files.length:0}; }"


def _validate_path(session: Any, raw: str) -> Path:
    candidate = Path(raw).expanduser().resolve()
    roots = tuple(Path(root).expanduser().resolve() for root in getattr(session.cfg, "upload_roots", ()) or ())
    if not roots:
        raise BadRequest("browser_upload 未配置上传根目录（ICODE_BROWSER_UPLOAD_ROOTS）")
    if not any(candidate == root or root in candidate.parents for root in roots):
        raise BadRequest("上传文件不在允许的根目录内")
    if not candidate.is_file():
        raise BadRequest("上传文件不存在或不是普通文件")
    return candidate


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    target = arguments["target"]
    path = _validate_path(session, arguments["path"])
    entry = await resolve_target(session, target)
    object_id = await session.resolve_object_id(entry)
    check = await call_value(session, object_id, _IS_FILE_INPUT_FN, cdp_session_id=entry.cdp_session_id)
    value = check.get("value") if isinstance(check, dict) else None
    if not isinstance(value, dict) or not value.get("ok"):
        raise BadRequest("目标不是可用的 file input")
    action_id = new_action_id()
    prepare_action(session, action_id, "browser_upload", f"prepared upload to {entry.token}")
    client = session.require_client()
    dialog_seq = getattr(session, "dialog_seq", 0)
    try:
        _, dialog = await send_watching_dialog(
            session,
            dialog_seq,
            lambda: client.send(
                "DOM.setFileInputFiles",
                {"files": [str(path)], "objectId": object_id},
                session_id=entry.cdp_session_id or session.session_id,
                timeout=session.cfg.cdp_timeout,
            ),
        )
    except ConnectionClosed:
        raise
    except (CDPTimeout, BrowserError) as exc:
        text = (
            f"上传可能已发出（{entry.token}，setFileInputFiles 失败：{type(exc).__name__}）\n"
            "status=uncertain retryable=false；请 snapshot 核对文件字段后决定是否重试\n"
            f"{evidence_line(session, action_id)}"
        )
        record_action(session, action_id, "browser_upload", "uncertain", text)
        return [text_block(text)]
    mark_dispatched(session, action_id, "browser_upload", f"dispatched upload to {entry.token}")
    session.mark_dirty()
    if dialog is not None:
        text = (
            f"已发送上传到 {entry.token}：{path.name}\n"
            f"{dialog.fields()}\n"
            "对话框出现：页面脚本暂停，上传结果未确认（不要重复上传）\n"
            "status=blocked retryable=false\n"
            "请先调用 browser_dialog（action=accept 或 dismiss），再 browser_snapshot 复核\n"
            f"{evidence_line(session, action_id)}"
        )
        record_action(session, action_id, "browser_upload", "blocked", text)
        return [text_block(text)]
    # The send returning is not the business result: read the field back.
    try:
        verified = await call_value(session, object_id, _FILE_COUNT_FN, cdp_session_id=entry.cdp_session_id)
    except ConnectionClosed:
        raise
    except (CDPTimeout, BrowserError) as exc:
        text = (
            f"已发送上传到 {entry.token}：{path.name}，但回读失败（{type(exc).__name__}，页面可能已更新或卸载）\n"
            "status=uncertain retryable=false；请先 snapshot 核对页面状态，不要直接重试\n"
            f"{evidence_line(session, action_id)}"
        )
        record_action(session, action_id, "browser_upload", "uncertain", text)
        return [text_block(text)]
    value = verified.get("value") if isinstance(verified, dict) else None
    count = int(value.get("count", 0)) if isinstance(value, dict) else 0
    if count < 1:
        text = (
            f"已发送上传到 {entry.token}：{path.name}，但字段回读为空\n"
            "status=uncertain retryable=false；请先 snapshot 核对该字段，不要直接重试\n"
            f"{evidence_line(session, action_id)}"
        )
        record_action(session, action_id, "browser_upload", "uncertain", text)
        return [text_block(text)]
    session.note_success(session.target_id)
    text = f"已上传文件到 {entry.token}：{path.name}（字段回读 {count} 个文件）\n{evidence_line(session, action_id)}"
    record_action(session, action_id, "browser_upload", "succeeded", text)
    return [text_block(text)]
