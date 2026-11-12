# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for converting private rejection metadata into public result metadata."""

from __future__ import annotations

from typing import Any

from chrys.foundation.tool_result_metadata import (
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_ERROR_MESSAGE_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
)
from chrys.service.agent_middleware._metadata_keys import (
    _HOOK_CONFIRMATION_KEY,
    _REJECTION_MESSAGE_KEY,
    _REJECTION_SOURCE_KEY,
)
from chrys.service.agent_middleware.events.result_persistence import RESULT_APPROVAL_METADATA_KEY


def rejection_result_metadata(metadata: Any) -> dict[str, Any]:
    """Return public tool-result metadata for a user or hook rejection."""
    source = rejection_source(metadata) or "user"
    message = rejection_message(metadata, source)
    result: dict[str, Any] = {
        TOOL_FAILED_METADATA_KEY: True,
        TOOL_ERROR_MESSAGE_METADATA_KEY: message,
    }
    if source == "hook":
        result[TOOL_ERROR_KIND_METADATA_KEY] = "hook_denied"
    elif source in {"prejudge", "approval_invalidated"}:
        result[TOOL_ERROR_KIND_METADATA_KEY] = "prejudge_blocked" if source == "prejudge" else "approval_invalidated"
    else:
        result[RESULT_APPROVAL_METADATA_KEY] = "user_rejected"
        result[TOOL_ERROR_KIND_METADATA_KEY] = "approval_rejected"
    return result


def rejection_source(metadata: Any) -> str:
    """Return the private rejection source when present."""
    if not isinstance(metadata, dict):
        return ""
    source = metadata.get(_REJECTION_SOURCE_KEY)
    return source if source in {"user", "hook", "prejudge", "approval_invalidated"} else ""


def hook_confirmation(metadata: Any) -> str:
    """How the hook's ``ask`` that stopped the call ended (``declined``, ``timed_out`` or
    ``unavailable``); empty when no ``ask`` stopped it."""
    if not isinstance(metadata, dict):
        return ""
    outcome = metadata.get(_HOOK_CONFIRMATION_KEY)
    return outcome if isinstance(outcome, str) else ""


def rejection_message(metadata: Any, source: str) -> str:
    """Return the private rejection message or a source-specific fallback."""
    if isinstance(metadata, dict):
        message = metadata.get(_REJECTION_MESSAGE_KEY)
        if isinstance(message, str) and message:
            return message
    if source == "hook":
        return "Tool execution was blocked by hook."
    if source == "prejudge":
        return "Tool execution was blocked by structured pre-judge."
    if source == "approval_invalidated":
        return "Tool arguments changed after approval."
    return "Tool execution was rejected by user."
