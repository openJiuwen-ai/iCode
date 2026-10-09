# ruff: noqa: RUF001, RUF002, RUF003
"""tool-detail/save + update 组装（方案 §4.2、§4.4）。

- Start（审批后、执行前）→ ``save`` + ``update(PENDING=3)``；
- Result → ``update(成功=1 / 失败=2 / 拒绝=4)``，写类工具附 difflib 行数；
- 输入触发（skill 引用命中）→ 单条 ``save``（funcType=0，saveOnly，不更新）。

save→update 的保序由 ``TelemetryHttpClient`` 的单 worker 串行队列天然保证。
取消的工具调用不发 Result（``CancelledError`` 路径）→ save 停在 PENDING，
M2 明确容忍（方案 §8-4）。
"""

from __future__ import annotations

import difflib
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from chrys.aixcoding.telemetry.outcome import classify_result_metadata
from chrys.aixcoding.telemetry.reporters import remember_bounded
from chrys.aixcoding.telemetry.types import (
    TOOL_DETAIL_SAVE,
    TOOL_DETAIL_UPDATE,
    CodeStatus,
    FuncType,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from chrys.foundation.events.types import InvocationToolCallResult, InvocationToolCallStart

logger = logging.getLogger(__name__)

# -- 参数白名单（方案决策 #6：默认只放行"读了哪个 X"类短参数；全量模式可切） -------
# 对齐 aixcoding ``toolUseReports.ts``（ReadFile/ReadSkill），按 iCode 工具集调整：
# read_file → filepath；load_skill → skill_name。funcName 恒为工具原名。
VALUE_ARG_FIELD_BY_TOOL: dict[str, str] = {
    "read_file": "filepath",
    "load_skill": "skill_name",
}

_FULL_VALUE_MAX_CHARS = 2000
"""全量模式（``toolParamMode: full``）value 截断上限，对齐 pi-acp toolParam。"""


def func_type_for_kind(tool_kind: str) -> int:
    """tool_kind → csas funcType（skill=0 / MCP=1 / 内置=3）。"""
    from chrys.foundation.tool_kinds import KIND_MCP, KIND_SKILL

    if tool_kind == KIND_SKILL:
        return FuncType.SKILL
    if tool_kind == KIND_MCP:
        return FuncType.MCP
    return FuncType.BUILTIN


def pick_value(tool_name: str, args: Mapping[str, Any], *, full_mode: bool) -> str | None:
    """按白名单挑选调用参数 ``value``；全量模式输出截断后的 args JSON。"""
    if full_mode:
        try:
            text = json.dumps(args, ensure_ascii=False, default=str)
        except TypeError, ValueError:
            return None
        return text[:_FULL_VALUE_MAX_CHARS] or None
    field = VALUE_ARG_FIELD_BY_TOOL.get(tool_name)
    if field is None:
        return None
    value = args.get(field)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return str(value).strip()[:_FULL_VALUE_MAX_CHARS] or None


def line_counts(result_metadata: Mapping[str, Any]) -> dict[str, int]:
    """写类工具的 original/added/deleted 行数（SnapshotStore difflib，随终态上报）。"""
    snapshot = result_metadata.get("file_snapshot")
    before_text = getattr(snapshot, "before_text", None)
    after_text = getattr(snapshot, "after_text", None)
    if not isinstance(before_text, str) or not isinstance(after_text, str):
        return {}
    before_lines = before_text.splitlines()
    added = deleted = 0
    for diff_line in difflib.ndiff(before_lines, after_text.splitlines()):
        if diff_line.startswith("+ "):
            added += 1
        elif diff_line.startswith("- "):
            deleted += 1
    return {"originalLines": len(before_lines), "addedLines": added, "deletedLines": deleted}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ToolDetailReporter:
    """组装并提交 tool-detail 报文（save / update / 输入触发 save）。"""

    def __init__(self, submit: Callable[[str, Mapping[str, object]], None]) -> None:
        self._submit = submit
        self._started_at: dict[str, datetime] = {}

    def on_start(self, event: InvocationToolCallStart) -> None:
        from chrys.aixcoding.config import load_settings
        from chrys.aixcoding.telemetry.llm_telemetry import resolve_call
        from chrys.aixcoding.telemetry.reporters import common_fields

        remember_bounded(self._started_at, event.call_id, event.timestamp)
        request = resolve_call(event.provider_call_id, event.session_id)
        payload: dict[str, Any] = {
            "funcId": event.call_id,
            "funcName": event.tool_name,
            "funcType": func_type_for_kind(event.tool_kind),
            "sessionId": event.session_id,
            "spanId": event.origin.invocation_id,
        }
        if request is not None:
            payload["requestId"] = request[0]
            if request[1]:
                payload["parentSpanId"] = request[1]
        value = pick_value(event.tool_name, event.args, full_mode=load_settings().tool_param_mode == "full")
        if value is not None:
            payload["value"] = value
        from chrys.aixcoding.context import current_function_name

        if agent_name := current_function_name():
            payload["agentName"] = agent_name
        payload.update(common_fields())
        self._submit(TOOL_DETAIL_SAVE, payload)
        self._submit(
            TOOL_DETAIL_UPDATE,
            {
                "toolUseId": event.call_id,
                "codeStatus": CodeStatus.PENDING,
                "executionStartedAt": _iso(event.timestamp),
            },
        )

    def on_result(self, event: InvocationToolCallResult) -> None:
        started = self._started_at.pop(event.call_id, None)
        classification = classify_result_metadata(event.metadata)
        payload: dict[str, Any] = {
            "toolUseId": event.call_id,
            "codeStatus": classification.code_status,
            "executionDurationMs": event.duration_ms,
        }
        if started is not None:
            payload["executionStartedAt"] = _iso(started)
        if event.timestamp is not None:
            payload["executionFinishedAt"] = _iso(event.timestamp)
        if classification.failure_type is not None:
            payload["failureType"] = classification.failure_type
        if classification.rejected or classification.code_status != CodeStatus.SUCCESS:
            error_text = _error_text(event)
            if error_text:
                payload["toolErrorMessage"] = error_text
        payload.update(line_counts(event.metadata))
        self._submit(TOOL_DETAIL_UPDATE, payload)

    def record_invocation(self, skill_name: str, session_id: str | None) -> None:
        """输入触发：用户输入的 slash skill 引用命中 → 单条 save（funcType=0）。"""
        from chrys.aixcoding.telemetry.llm_telemetry import resolve_call
        from chrys.aixcoding.telemetry.reporters import common_fields

        payload: dict[str, Any] = {
            "funcName": skill_name,
            "funcType": FuncType.SKILL,
            "sessionId": session_id,
        }
        request = resolve_call("", session_id)
        if request is not None:
            payload["requestId"] = request[0]
        from chrys.aixcoding.context import current_function_name

        if agent_name := current_function_name():
            payload["agentName"] = agent_name
        payload.update(common_fields())
        self._submit(TOOL_DETAIL_SAVE, payload)


def _error_text(event: InvocationToolCallResult) -> str | None:
    text = (event.result or "").strip()
    return text[:_FULL_VALUE_MAX_CHARS] or None
