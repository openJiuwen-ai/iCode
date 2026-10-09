# ruff: noqa: RUF002, RUF003
"""csas telemetry 契约常量：端点、枚举与 payload 结构（方案 §四 / 附录 B）。"""

from __future__ import annotations

from enum import IntEnum
from typing import TypedDict

# -- 端点（相对 report 基址，即 ``{base}/telemetry/api/v1`` 之后的部分） ----------

TOOL_DETAIL_SAVE = "tool-detail/save"
TOOL_DETAIL_BATCH_SAVE = "tool-detail/batch-save"
TOOL_DETAIL_UPDATE = "tool-detail/update"
AI_CODE_SAVE = "ai-code/save"
EVENT_REACTION_SAVE = "event-reaction/save"


# -- funcType（skill=0 / MCP=1 / 内置=3，对齐 pi-acp 与 aixcoding 枚举） ---------


class FuncType(IntEnum):
    SKILL = 0
    MCP = 1
    BUILTIN = 3


# -- codeStatus 五态（方案 §4.3；五态映射待产品/后端确认，见方案 §8-3） ----------


class CodeStatus(IntEnum):
    SUCCESS = 1
    FAILED = 2
    PENDING = 3
    USER_REJECTED = 4
    USER_APPROVED = 5


class FailureType:
    """tool-detail update 的 failureType 字面量。"""

    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ERROR = "error"


# -- 渠道（方案决策 #7；channelType 枚举值待后端确认，见方案 §8-1） --------------

CHANNEL_TYPE_CLI = "cli"
CHANNEL_TYPE_DESKTOP = "desktop"


# -- llm-call 搭车 payload（随模型请求体 ``telemetry`` 键下发，方案 §4.1） -------


class LlmTelemetryPayload(TypedDict, total=False):
    requestId: str
    sessionId: str
    spanId: str
    parentSpanId: str
    eventType: str  # 恒 "llm"
    eventSubType: str  # "agent"（主对话）| "system"（side call）
    channelType: str
    channelName: str
    channelVersion: str
    pluginVersion: str
    projectName: str
    gitRemote: str
    gitBranch: str
    gitRevision: str
    gitOwner: str
    gitRepo: str
