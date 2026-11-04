# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent-level middleware for event publishing and execution control.

Provides:
- ToolEventMiddleware: Publishes InvocationToolCallStart/InvocationToolCallResult on each tool invocation.
- SubAgentEventMiddleware: Publishes InvocationToolCallStart/InvocationToolCallResult for sub-agent tools.
- ApprovalMiddleware: Gates tool execution with user approval inside the
  Chrys tool loop (no snapshot/restore needed).
- InterruptMiddleware: Checks for interrupt signals between tool calls.
- AskUserMiddleware: Intercepts ask_user tool calls to show a dialog.
- SleepMiddleware: Intercepts sleep tool calls so the frontend can skip them.
- ResponseBatchCounter: Numbers a main pass's tool-calling responses.
"""

from chrys.foundation.io.result_content import extract_result_images, extract_result_text
from chrys.service.agent_middleware._metadata_keys import (
    _APPROVAL_MODIFIED_ARGS_KEY,
    _APPROVAL_REJECTED_KEY,
    _CALL_ID_KEY,
    _SHORT_ID_LEN,
    _SLEEP_INTERRUPTED_KEY,
    _SLEEP_SKIPPED_KEY,
)
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.agent_middleware.control.ask_user import AskUserMiddleware
from chrys.service.agent_middleware.control.interrupt import InterruptMiddleware
from chrys.service.agent_middleware.control.sleep import SleepMiddleware
from chrys.service.agent_middleware.events.intermediate_text import ResponseBatchCounter
from chrys.service.agent_middleware.events.sub_agent_events import SubAgentEventMiddleware, SubAgentStatsMiddleware
from chrys.service.agent_middleware.events.tool_events import ToolEventMiddleware
from chrys.service.mutations.pipeline import _read_file_snapshot, _read_shell_snapshots
from chrys.service.mutations.tool_names import _FILE_TOOLS, _IMPLICIT_WRITE_TOOLS

__all__ = [
    "_APPROVAL_MODIFIED_ARGS_KEY",
    "_APPROVAL_REJECTED_KEY",
    "_CALL_ID_KEY",
    "_FILE_TOOLS",
    "_IMPLICIT_WRITE_TOOLS",
    "_SHORT_ID_LEN",
    "_SLEEP_INTERRUPTED_KEY",
    "_SLEEP_SKIPPED_KEY",
    "ApprovalMiddleware",
    "AskUserMiddleware",
    "InterruptMiddleware",
    "ResponseBatchCounter",
    "SleepMiddleware",
    "SubAgentEventMiddleware",
    "SubAgentStatsMiddleware",
    "ToolEventMiddleware",
    "_read_file_snapshot",
    "_read_shell_snapshots",
    "extract_result_images",
    "extract_result_text",
]
