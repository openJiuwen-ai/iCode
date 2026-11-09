# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for selecting tool-result metadata that belongs in session history."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from chrys.foundation.tool_call_context import TOOL_CALL_CONTEXT_METADATA_KEY
from chrys.foundation.tool_result_metadata import (
    PROCESS_EXIT_CODE_METADATA_KEY,
    PROCESS_TIMED_OUT_METADATA_KEY,
    PROCESS_TIMEOUT_SECONDS_METADATA_KEY,
    SHELL_BACKGROUND_TASK_METADATA_KEY,
    SHELL_EXIT_CODE_METADATA_KEY,
    SHELL_TIMED_OUT_METADATA_KEY,
    SHELL_TIMEOUT_SECONDS_METADATA_KEY,
    TOOL_ERROR_CODE_METADATA_KEY,
    TOOL_ERROR_DETAILS_METADATA_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_ERROR_MESSAGE_METADATA_KEY,
    TOOL_ERROR_RETRYABLE_METADATA_KEY,
    TOOL_ERRORED_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
    TOOL_INTERRUPTED_METADATA_KEY,
    TOOL_POST_PROCESSING_INTERRUPTED_METADATA_KEY,
    TOOL_RESULT_METADATA_KEY,
    WEB_FETCH_FINAL_URL_METADATA_KEY,
    WEB_SEARCH_METADATA_KEY,
)
from chrys.foundation.util.sub_agent_context import SUB_AGENT_PROGRESS_METADATA_KEY

if TYPE_CHECKING:
    from chrys.kernel.middleware import FunctionInvocationContext

RESULT_APPROVAL_METADATA_KEY = "approval"
RESULT_SLEEP_SKIPPED_METADATA_KEY = "sleep_skipped"
RESULT_SLEEP_INTERRUPTED_METADATA_KEY = "sleep_interrupted"
RESULT_SUB_AGENT_INVOCATION_ID_METADATA_KEY = "sub_agent_invocation_id"
RESULT_SUB_AGENT_LOG_FILE_METADATA_KEY = "sub_agent_log_file"
RESULT_SUB_AGENT_AUDIT_COMPLETE_METADATA_KEY = "sub_agent_audit_complete"

PERSISTED_RESULT_METADATA_KEYS = frozenset(
    {
        RESULT_APPROVAL_METADATA_KEY,
        RESULT_SLEEP_SKIPPED_METADATA_KEY,
        RESULT_SLEEP_INTERRUPTED_METADATA_KEY,
        RESULT_SUB_AGENT_INVOCATION_ID_METADATA_KEY,
        RESULT_SUB_AGENT_LOG_FILE_METADATA_KEY,
        RESULT_SUB_AGENT_AUDIT_COMPLETE_METADATA_KEY,
        # A restored sub-agent card shows its run's counters from this.
        SUB_AGENT_PROGRESS_METADATA_KEY,
        PROCESS_EXIT_CODE_METADATA_KEY,
        PROCESS_TIMED_OUT_METADATA_KEY,
        PROCESS_TIMEOUT_SECONDS_METADATA_KEY,
        SHELL_BACKGROUND_TASK_METADATA_KEY,
        SHELL_EXIT_CODE_METADATA_KEY,
        SHELL_TIMED_OUT_METADATA_KEY,
        SHELL_TIMEOUT_SECONDS_METADATA_KEY,
        TOOL_FAILED_METADATA_KEY,
        TOOL_ERROR_KIND_METADATA_KEY,
        TOOL_ERROR_MESSAGE_METADATA_KEY,
        TOOL_ERROR_CODE_METADATA_KEY,
        TOOL_ERROR_RETRYABLE_METADATA_KEY,
        TOOL_ERROR_DETAILS_METADATA_KEY,
        TOOL_ERRORED_METADATA_KEY,
        TOOL_INTERRUPTED_METADATA_KEY,
        TOOL_POST_PROCESSING_INTERRUPTED_METADATA_KEY,
        # The web cards render their sources from these, never from the result text.
        WEB_SEARCH_METADATA_KEY,
        WEB_FETCH_FINAL_URL_METADATA_KEY,
    }
)
"""Tool result metadata keys that should survive session replay."""


def persistable_result_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of result metadata that belongs in session history."""
    return {key: metadata[key] for key in PERSISTED_RESULT_METADATA_KEYS if key in metadata}


def write_result_carriage(
    context: FunctionInvocationContext,
    *,
    metadata: dict[str, Any],
    tool_context: dict[str, Any] | None = None,
) -> None:
    """Write result metadata + call provenance carriage into the invocation context.

    The kernel tool loop folds ``TOOL_RESULT_METADATA_KEY`` into the result
    Content at construction — the only moment the result's identity is
    unambiguous — and merges ``TOOL_CALL_CONTEXT_METADATA_KEY`` into the call
    content post-pipeline at subkey granularity. Direct carriage replaces the
    drained-record positional re-association that could attach one call's
    metadata to another call's result.
    """
    if not isinstance(context.metadata, dict):
        context.metadata = {}
    persisted = persistable_result_metadata(metadata)
    if persisted:
        context.metadata[TOOL_RESULT_METADATA_KEY] = persisted
    if tool_context:
        context.metadata[TOOL_CALL_CONTEXT_METADATA_KEY] = dict(tool_context)
