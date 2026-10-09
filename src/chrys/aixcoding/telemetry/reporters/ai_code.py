# ruff: noqa: RUF001, RUF002
"""ai-code/save 组装（方案 §4.3）：写类工具成功后的 AI 代码块上报。

数据源：``write_file`` / ``edit_file``（``_FILE_TOOLS``；shell 隐式写不报）。
before/after 全文取 Result 事件 ``metadata["file_snapshot"]``（SnapshotStore
解码的 ``FileMutationTextSnapshot``）；blocks 对齐 pi-acp ``aiCodeBlocks``
（整文件一个 block；rangeStart 定位旧文中新文本首行，找不到回退 1）。

codeStatus 采纳语义（§4.3；五态映射待 §8-3 定稿）：
- 拒绝（Result metadata ``approval=user_rejected``）→ 4——注意拒绝时工具不
  执行、无 mutation，本 reporter 不触发；4 落在 tool-detail update（M2 已实现）；
- 批准且经用户对话框（请求时 mode=manual）→ 5；
- judge 批准（mode=auto）/ bypass / 无审批 → 1。

mode 真值源与批准事件均来自 EventBus（``ApprovalModeUpdated`` /
``ApprovalRequest`` / ``ApprovalResponse``），由 ``ApprovalTracker`` 维护。
批量缓冲对齐 pi-acp batchSize 机制（``BatchBuffer`` 满 N 条或 T 秒 flush）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.aixcoding.telemetry.reporters import remember_bounded
from chrys.aixcoding.telemetry.types import CodeStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.foundation.events.types import (
        ApprovalModeUpdated,
        ApprovalRequest,
        ApprovalResponse,
        InvocationToolCallResult,
        InvocationToolCallStart,
    )

logger = logging.getLogger(__name__)

FILE_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
"""触发 ai-code 上报的写类工具（对齐 service 层 ``_FILE_TOOLS``；shell 隐式写不报）。"""

_MODE_MANUAL = "manual"


def ai_code_blocks(before_text: str | None, after_text: str) -> list[dict[str, int | str]]:
    """before/after → blocks（整文件一个 block，对齐 pi-acp 语义）。"""
    if not after_text:
        return []
    new_line_count = len(_split_lines(after_text))
    range_start = 1
    if before_text is not None:
        index = before_text.find(after_text)
        if index >= 0:
            range_start = before_text[:index].count("\n") + 1
    return [{"snippet": after_text, "rangeStart": range_start, "rangeEnd": range_start + new_line_count - 1}]


def _split_lines(text: str) -> list[str]:
    """按 CRLF/LF 分行（尾部保留空串，对齐 JS ``split(/\\r?\\n/)`` 行数口径）。"""
    return text.replace("\r\n", "\n").split("\n")


def _relative_filepath(path: str) -> str:
    """git 相对路径（当前以 cwd 为基准；越界回退绝对路径原文）。"""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve())).replace("\\", "/")
    except ValueError, OSError:
        return path


class ApprovalTracker:
    """从审批事件流维护 call_id → 采纳语义 codeStatus 所需的事实。

    四张表均为有界记忆（超限丢最旧）——长驻进程防泄漏。
    """

    def __init__(self) -> None:
        self._modes: dict[str, str] = {}
        self._request_mode: dict[str, str] = {}
        self._request_call_id: dict[str, str] = {}
        self._approved: dict[str, bool] = {}

    def on_mode_updated(self, event: ApprovalModeUpdated) -> None:
        if event.session_id:
            remember_bounded(self._modes, event.session_id, event.mode)

    def on_request(self, event: ApprovalRequest) -> None:
        remember_bounded(self._request_mode, event.request_id, self._modes.get(event.session_id or "", ""))
        if event.call_id:
            remember_bounded(self._request_call_id, event.request_id, event.call_id)

    def on_response(self, event: ApprovalResponse) -> None:
        remember_bounded(self._approved, event.request_id, event.approved)

    def adopted_code_status(self, call_id: str, session_id: str | None, *, rejected: bool) -> int:
        """写类工具执行成功后的采纳 codeStatus（5=用户批准 / 1=其他）。"""
        if rejected:
            return CodeStatus.USER_REJECTED
        for request_id, approved in self._approved.items():
            if approved and self._request_call_id.get(request_id) == call_id:
                if self._request_mode.get(request_id) == _MODE_MANUAL:
                    return CodeStatus.USER_APPROVED
                return CodeStatus.SUCCESS
        return CodeStatus.SUCCESS


class AiCodeReporter:
    """组装 ai-code/save 报文并经批量缓冲提交。"""

    def __init__(self, add: Callable[[Mapping[str, object]], None]) -> None:
        self._add = add
        self._args_by_call: dict[str, dict[str, Any]] = {}

    def on_start(self, event: InvocationToolCallStart) -> None:
        if event.tool_name in FILE_WRITE_TOOLS:
            remember_bounded(self._args_by_call, event.call_id, dict(event.args or {}))

    def on_result(
        self,
        event: InvocationToolCallResult,
        *,
        code_status: int,
        errored: bool,
    ) -> None:
        args = self._args_by_call.pop(event.call_id, None)
        if args is None or event.tool_name not in FILE_WRITE_TOOLS or errored:
            return
        snapshot = event.metadata.get("file_snapshot")
        before_text = getattr(snapshot, "before_text", None)
        after_text = getattr(snapshot, "after_text", None)
        if not isinstance(after_text, str) or not after_text:
            return
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return

        from chrys.aixcoding.telemetry.llm_telemetry import resolve_call
        from chrys.aixcoding.telemetry.reporters import common_fields

        payload: dict[str, Any] = {
            "reportId": str(uuid.uuid4()),
            "filepath": _relative_filepath(path),
            "blocks": ai_code_blocks(before_text if isinstance(before_text, str) else None, after_text),
            "sourceType": "edit",
            "sessionId": event.session_id,
            "codeStatus": code_status,
            "spanId": event.origin.invocation_id,
        }
        request = resolve_call(event.provider_call_id, event.session_id)
        if request is not None:
            payload["requestId"] = request[0]
        from chrys.aixcoding.context import current_function_name

        if agent_name := current_function_name():
            payload["agentName"] = agent_name
        payload.update(
            {
                key: value
                for key, value in (
                    ("remoteUrl", _git_field("git_remote")),
                    ("branch", _git_field("git_branch")),
                    ("gitUserName", _git_field("git_user_name")),
                    ("gitUserEmail", _git_field("git_user_email")),
                )
                if value
            }
        )
        payload.update(common_fields())
        self._add(payload)


def _git_field(name: str) -> str | None:
    from chrys.aixcoding.git_info import collect_git_info

    info = collect_git_info(Path.cwd())
    if info is None:
        return None
    return getattr(info, name, None)
