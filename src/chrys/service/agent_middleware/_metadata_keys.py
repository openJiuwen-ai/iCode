# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared constants for agent-level middleware."""

from chrys.foundation.tool_invocation_order import (
    TOOL_INVOCATION_ORDER_KEY as TOOL_INVOCATION_ORDER_KEY,
)

_SHORT_ID_LEN = 12
"""Characters for shortened UUID hex identifiers."""

_APPROVAL_REJECTED_KEY = "_approval_rejected"
"""Metadata key set by ``ApprovalMiddleware`` on rejected tools.

``ToolEventMiddleware`` checks ``context.metadata[key]`` instead of
matching the result string, keeping detection decoupled from the
user-facing error message (which may change or be localised).
"""

_REJECTION_SOURCE_KEY = "_rejection_source"
"""Private metadata key identifying whether rejection came from user or hook."""

_REJECTION_MESSAGE_KEY = "_rejection_message"
"""Private metadata key carrying the structured rejection message."""

_APPROVAL_MODIFIED_ARGS_KEY = "_approval_modified_args"
"""Metadata key carrying approval-edited tool arguments."""

_HOOK_OPERATION_KEY = "_chrys_hook_operation"
"""Metadata key carrying the call's ``HookOperation``: the version ``before_tool_call`` hooks
checked, which the innermost commit middleware takes before the tool runs. Never persisted."""

_HOOK_CONFIRMATION_KEY = "_hook_confirmation"
"""Metadata key set when a hook's ``ask`` stopped the call (the user did not approve, or nobody could be asked)."""

_BEFORE_TOOL_CONTEXT_KEY = "_chrys_before_tool_context"
"""Metadata key carrying the ``extra_context`` that ``before_tool_call`` hooks returned for the
version of the call that runs, appended to its result ahead of ``after_tool_call`` context.
Absent when the call was blocked. Never persisted."""

_SLEEP_SKIPPED_KEY = "_sleep_skipped"
"""Metadata key set by ``SleepMiddleware`` when the user skips a sleep."""

_SLEEP_INTERRUPTED_KEY = "_sleep_interrupted"
"""Metadata key set by ``SleepMiddleware`` when a user interrupt resolves a sleep."""

_CALL_ID_KEY = "_chrys_call_id"
"""Metadata key carrying the chrys tool-call id.

``ToolEventMiddleware`` mints the id at the start of every invocation
and writes it here so downstream middleware (hooks, approval, sub-agent
correlation) and persisted ``ToolCallStart`` / ``ToolCallResult``
events all agree on the same identifier.  Always 12 lower-hex chars
(see :data:`_SHORT_ID_LEN`).
"""

_TOOL_INVOCATION_ORDER_KEY = TOOL_INVOCATION_ORDER_KEY
"""Metadata key carrying the current-run tool invocation ordinal.

The kernel tool loop stamps it at response arrival (the single authoritative
producer, :mod:`chrys.foundation.tool_invocation_order`); the event
middleware only fall back to local follower counters for direct-pipeline
harnesses that bypass the kernel loop.
"""
