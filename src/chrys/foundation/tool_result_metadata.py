# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared keys and legacy text helpers for tool-result status."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from chrys.foundation.hosted_tools import HostedToolStatus, normalize_hosted_tool_status
from chrys.foundation.tool_kinds import (
    KIND_ASK_USER,
    KIND_CONTEXT,
    KIND_DOC_CONVERTER,
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_SEARCH,
    KIND_SHELL,
    KIND_SKILL,
    KIND_SLEEP,
    KIND_SUB_AGENT,
    KIND_WEB_FETCH,
    KIND_WEB_SEARCH,
)

tool_payload_observation: ContextVar[dict[str, object] | None] = ContextVar("tool_payload_observation", default=None)
"""Trajectory-only buffer for the active tool call's payload facts (truncation, spill artifact).

Nothing here is persisted on the result; the tool event middleware folds it
into ``tool.payload.observed``. It lives at the foundation layer because the
kernel's own result ceiling is the last thing to bound a result, and its
truncation would otherwise go unobserved.
"""


def record_payload_truncation(
    original_text: str,
    *,
    original_bytes: int | None = None,
    artifact_name: str | None = None,
) -> None:
    """Note that the active tool call's output was bounded before reaching the model.

    *original_bytes* stands in for *original_text*'s size when the tool saw
    more output than it kept.
    """
    observation = tool_payload_observation.get(None)
    if observation is None:
        return
    if original_bytes is None:
        original_bytes = len(original_text.encode("utf-8", errors="backslashreplace"))
    # First writer wins the size: a later bounding pass only ever sees text
    # that was already shortened, and the reader wants what the tool produced.
    observation.setdefault("original_bytes", original_bytes)
    observation["truncated"] = True
    if artifact_name:
        observation["artifact_id"] = artifact_name


TOOL_RESULT_METADATA_KEY = "_chrys_tool_result_metadata"
"""Structured renderer metadata persisted on ``function_result`` contents.

Lives at the foundation layer so the kernel tool loop can fold the dict the
event middleware carried in ``FunctionInvocationContext.metadata`` (same key,
same meaning) into the result content at construction — the only moment the
result's identity is unambiguous.
"""

TOOL_ERRORED_METADATA_KEY = "errored"
"""True when the tool invocation failed outside normal tool-result semantics."""

TOOL_FAILED_METADATA_KEY = "failed"
"""True when a tool returned a normal, structured failure result."""

TOOL_INTERRUPTED_METADATA_KEY = "interrupted"
"""True when cancellation answered an in-flight invocation."""

TOOL_POST_PROCESSING_INTERRUPTED_METADATA_KEY = "interrupted_post_processing"
"""True when the tool returned but cancellation interrupted result processing."""

TOOL_ERROR_KIND_METADATA_KEY = "tool_error_kind"
"""Stable category for a structured tool failure."""

TOOL_ERROR_MESSAGE_METADATA_KEY = "tool_error_message"
"""Human-readable structured tool failure message, without the ``Error:`` prefix."""

TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY = "failure_text_synthesized"
"""True when Chrys synthesized a provider-hosted tool's fallback failure text."""

TOOL_ERROR_CODE_METADATA_KEY = "tool_error_code"
"""Optional provider/tool-specific structured error code."""

TOOL_ERROR_RETRYABLE_METADATA_KEY = "tool_error_retryable"
"""Optional structured retryability hint for a tool failure."""

TOOL_ERROR_DETAILS_METADATA_KEY = "tool_error_details"
"""Optional JSON-safe structured details for a tool failure."""

PROCESS_EXIT_CODE_METADATA_KEY = "process_exit_code"
"""Integer process exit code for generic subprocess-backed tool results."""

PROCESS_TIMED_OUT_METADATA_KEY = "process_timed_out"
"""True when a generic subprocess-backed tool result timed out."""

PROCESS_TIMEOUT_SECONDS_METADATA_KEY = "process_timeout_seconds"
"""Configured timeout, in seconds, for generic subprocess-backed tool results."""

SHELL_EXIT_CODE_METADATA_KEY = "shell_exit_code"
"""Integer process exit code for shell tool results."""

SHELL_TIMED_OUT_METADATA_KEY = "shell_timed_out"
"""True when the shell command was killed by Chrys timeout handling."""

SHELL_TIMEOUT_SECONDS_METADATA_KEY = "shell_timeout_seconds"
"""Configured timeout, in seconds, for timed-out shell commands."""

SHELL_BACKGROUND_TASK_METADATA_KEY = "shell_background_task"
"""The background task a shell command moved to (``id``, ``pid``, ``trigger``, ``log``), when it did."""

PARTIAL_OUTPUT_LABEL = "[partial output]"
"""The line that opens what a timed-out command or script printed, under its error line."""

WEB_SEARCH_METADATA_KEY = "web_search"
"""Provider id and result sources (``title`` and ``url`` only) for the web search card."""

WEB_SEARCH_TITLE_MAX_CHARS = 300
"""Longest source title the web search card shows, and so the longest one recorded."""

WEB_FETCH_FINAL_URL_METADATA_KEY = "web_fetch_final_url"
"""The URL a web fetch finally read, after same-site redirects, for the web fetch card."""

_RESULT_EXIT_CODE_SUFFIX_RE = re.compile(r"\n?\[exit_code:\s*(-?\d+)\][ \t\r\n]*$")

ERROR_TEXT_TOOL_KINDS = frozenset(
    {
        KIND_ASK_USER,
        KIND_CONTEXT,
        KIND_DOC_CONVERTER,
        KIND_FILESYSTEM_READ,
        KIND_FILESYSTEM_WRITE,
        KIND_SEARCH,
        KIND_WEB_SEARCH,
        KIND_WEB_FETCH,
        KIND_SHELL,
        KIND_SKILL,
        KIND_SLEEP,
        KIND_SUB_AGENT,
    }
)
"""Tool kinds where legacy ``Error:`` result text may indicate failure.

``KIND_SKILL`` / ``KIND_CONTEXT`` membership preserves failure-detection
parity: their tools sit in ``LEGACY_ERROR_TEXT_TOOL_NAMES`` (except
``list_compressed_contexts``), and the name fallback in
:func:`legacy_error_text_applies` only fires while the persisted kind is
empty — without these entries, stamping the new kinds would silently stop
treating legacy ``Error:`` text as failure on replay.
"""

LEGACY_ERROR_TEXT_TOOL_NAMES = frozenset(
    {
        "compress_context",
        "convert_document",
        "edit_file",
        "execute",
        "glob",
        "grep",
        "load_skill",
        "ask_user",
        "bash",
        "cmd",
        "csh",
        "dash",
        "fish",
        "git_bash",
        "ksh",
        "list_workspace_dirs",
        "powershell",
        "pwsh",
        "read_file",
        "read_skill_resource",
        "recall_context",
        "run_skill_script",
        "sh",
        "sleep",
        "tcsh",
        "view_image",
        "write_file",
        "zsh",
    }
)
"""Known Chrys-owned tool names kept for old session replay compatibility."""

REJECTION_ERROR_KINDS = frozenset({"approval_rejected", "hook_denied", "prejudge_blocked", "approval_invalidated"})
"""Structured failure kinds that represent rejected tool execution."""


def legacy_error_text_applies(tool_kind: str = "", tool_name: str = "") -> bool:
    """Return true when consumers may use legacy ``Error:`` text as a failure signal."""
    return tool_kind in ERROR_TEXT_TOOL_KINDS or (not tool_kind and tool_name in LEGACY_ERROR_TEXT_TOOL_NAMES)


def result_text_exit_code(result: str) -> int | None:
    """Return a shell-style trailing exit code, when one is present."""
    match = _RESULT_EXIT_CODE_SUFFIX_RE.search(result)
    if match is None:
        return None
    return int(match.group(1))


def result_text_without_exit_code(result: str) -> str:
    """Return result text with a trailing ``[exit_code: N]`` suffix removed."""
    return _RESULT_EXIT_CODE_SUFFIX_RE.sub("", result).rstrip("\n")


def shell_exit_code_from_metadata(metadata: Mapping[str, Any] | None) -> int | None:
    """Return a structured shell exit code, when metadata carries one."""
    return _int_metadata_value(metadata, SHELL_EXIT_CODE_METADATA_KEY)


def process_exit_code_from_metadata(metadata: Mapping[str, Any] | None) -> int | None:
    """Return a structured generic process exit code, when metadata carries one."""
    return _int_metadata_value(metadata, PROCESS_EXIT_CODE_METADATA_KEY)


def shell_timed_out_from_metadata(metadata: Mapping[str, Any] | None) -> bool:
    """Return true when structured metadata says the shell command timed out."""
    return _bool_metadata_true(metadata, SHELL_TIMED_OUT_METADATA_KEY)


def process_timed_out_from_metadata(metadata: Mapping[str, Any] | None) -> bool:
    """Return true when structured metadata says a generic process timed out."""
    return _bool_metadata_true(metadata, PROCESS_TIMED_OUT_METADATA_KEY)


def tool_result_metadata_is_rejected(metadata: Mapping[str, Any] | None) -> bool:
    """Return true when structured metadata says tool execution was rejected."""
    if not metadata:
        return False
    if metadata.get("approval") == "user_rejected":
        return True
    return metadata.get(TOOL_ERROR_KIND_METADATA_KEY) in REJECTION_ERROR_KINDS


def tool_result_metadata_failure_state(metadata: Mapping[str, Any] | None) -> bool | None:
    """Return structured failure state, or None when metadata has no answer."""
    if not metadata:
        return None
    if metadata.get(TOOL_ERRORED_METADATA_KEY) is True:
        return True
    if tool_result_metadata_is_rejected(metadata):
        return True
    failed = metadata.get(TOOL_FAILED_METADATA_KEY)
    if failed is True:
        return True
    if process_timed_out_from_metadata(metadata) or shell_timed_out_from_metadata(metadata):
        return True
    exit_code = process_exit_code_from_metadata(metadata)
    if exit_code is None:
        exit_code = shell_exit_code_from_metadata(metadata)
    if exit_code is not None:
        return exit_code != 0
    if failed is False:
        return False
    return None


def _int_metadata_value(metadata: Mapping[str, Any] | None, key: str) -> int | None:
    if not metadata:
        return None
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _bool_metadata_true(metadata: Mapping[str, Any] | None, key: str) -> bool:
    if not metadata:
        return False
    return metadata.get(key) is True


def canonical_tool_result_status(provider_status: str | None, metadata: Mapping[str, Any] | None) -> str:
    """Prefer a terminal provider status, then structured tool-result failure metadata."""
    status = normalize_hosted_tool_status(provider_status)
    if status in {HostedToolStatus.COMPLETED, HostedToolStatus.FAILED, HostedToolStatus.INTERRUPTED}:
        return status.value
    return "failed" if tool_result_metadata_failure_state(metadata) is True else "completed"
