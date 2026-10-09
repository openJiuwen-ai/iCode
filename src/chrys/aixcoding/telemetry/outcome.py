# ruff: noqa: RUF002
"""工具终态分类：Result 事件 metadata → csas 的 codeStatus / failureType（方案 §4.2）。

复用 foundation 的结构化判定（``tool_result_metadata_*``），不依赖 service 层
（aixcoding 分层约束：仅 kernel/foundation + 标准库）。

codeStatus 五态映射（user_approved=5 / judge+auto+bypass=1）待产品/后端确认
（§8-3），M2 的 tool-detail 更新暂用三值：成功=1、失败=2、审批拒绝=4。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from chrys.aixcoding.telemetry.types import CodeStatus, FailureType
from chrys.foundation.tool_result_metadata import (
    PROCESS_TIMED_OUT_METADATA_KEY,
    SHELL_TIMED_OUT_METADATA_KEY,
    TOOL_ERRORED_METADATA_KEY,
    tool_result_metadata_failure_state,
    tool_result_metadata_is_rejected,
)


@dataclass(frozen=True)
class ToolOutcomeClassification:
    """一次工具调用的 csas 终态分类。"""

    code_status: int
    failure_type: str | None = None
    rejected: bool = False


def classify_result_metadata(metadata: Mapping[str, Any]) -> ToolOutcomeClassification:
    """把 ``InvocationToolCallResult.metadata`` 归入 csas 终态。"""
    if tool_result_metadata_is_rejected(metadata):
        return ToolOutcomeClassification(CodeStatus.USER_REJECTED, rejected=True)
    timed_out = metadata.get(PROCESS_TIMED_OUT_METADATA_KEY) is True or bool(metadata.get(SHELL_TIMED_OUT_METADATA_KEY))
    errored = metadata.get(TOOL_ERRORED_METADATA_KEY) is True
    structured_failed = tool_result_metadata_failure_state(metadata) is True
    if timed_out:
        return ToolOutcomeClassification(CodeStatus.FAILED, FailureType.TIMEOUT)
    if errored or structured_failed:
        return ToolOutcomeClassification(CodeStatus.FAILED, FailureType.ERROR)
    return ToolOutcomeClassification(CodeStatus.SUCCESS)
