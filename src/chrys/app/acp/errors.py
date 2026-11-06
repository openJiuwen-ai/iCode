# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP error responses and failure reports that say what failed.

The SDK's factories name only the JSON-RPC error class ("Internal error",
"Invalid params"), and most clients show nothing but that message. The
factories here keep the class's code and put the cause in the message, while
``data`` keeps the structured detail. Every failure report also carries the
classifier's verdict in the same four fields (:func:`failure_fields`), so a
client can act on a failure without parsing its text.
"""

from __future__ import annotations

from typing import Any

from acp import RequestError

from chrys.app.acp.json_values import json_text, to_json_object
from chrys.foundation.errors.display import DISPLAY_WITH_HINT
from chrys.foundation.events.types import Error
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.failures import FailureFacts

INTERNAL_ERROR_CODE = -32603
INVALID_PARAMS_CODE = -32602
_INTERNAL_ERROR = "Internal error"
_INVALID_PARAMS = "Invalid params"
# A JSON-RPC message is one short sentence; ``data`` keeps the whole text.
_MAX_MESSAGE_CHARS = 500


def summary(text: str) -> str:
    """The first non-blank line of *text*, cut to a length a one-line message can show; empty for blank text.

    The text is made JSON-safe before the cut, so a cut never leaves half a surrogate pair.
    """
    line = next((line.strip() for line in json_text(text).splitlines() if line.strip()), "")
    if len(line) <= _MAX_MESSAGE_CHARS:
        return line
    return line[: _MAX_MESSAGE_CHARS - 1].rstrip() + "…"


def _detailed(code: int, generic: str, data: dict[str, Any]) -> RequestError:
    details = data.get("details")
    message = summary(details) if isinstance(details, str) else ""
    return RequestError(code, message or generic, to_json_object(data))


def internal_error(data: dict[str, Any]) -> RequestError:
    """-32603, whose message is ``data["details"]`` when it has one."""
    return _detailed(INTERNAL_ERROR_CODE, _INTERNAL_ERROR, data)


def invalid_params(data: dict[str, Any]) -> RequestError:
    """-32602, whose message is ``data["details"]`` when it has one."""
    return _detailed(INVALID_PARAMS_CODE, _INVALID_PARAMS, data)


def english_hint(display_message: MessageRef | None, display_hint: MessageRef | None) -> str | None:
    """What a classified failure means and what to do about it, in English as ACP output stays."""
    if display_message is None:
        return None
    message = format_message(display_message)
    if display_hint is None:
        return message
    return format_message(DISPLAY_WITH_HINT.bind(message=message, hint=format_message(display_hint)))


def failure_fields(
    facts: FailureFacts | None,
    display_message: MessageRef | None = None,
    display_hint: MessageRef | None = None,
    *,
    with_retryable: bool = True,
) -> dict[str, Any]:
    """The classifier's verdict as ACP reports it: ``kind``, ``retryable``, ``statusCode`` and ``hint``.

    Every field is present and null when the failure was not classified (one
    of Chrys's own checks, whose ``code`` and ``message`` say it all). A retry
    notice leaves ``retryable`` out: it is being retried whatever the verdict
    (a context overflow is resent once after compacting).
    """
    fields: dict[str, Any] = {
        "kind": str(facts.kind) if facts is not None else None,
        "retryable": facts.retryable if facts is not None else None,
        "statusCode": facts.status_code if facts is not None else None,
        "hint": english_hint(display_message, display_hint) if facts is not None else None,
    }
    if not with_retryable:
        del fields["retryable"]
    return fields


def turn_failed(error: Error, extra: dict[str, Any]) -> RequestError:
    """The error a prompt or retry answers when its turn failed: the cause as message, the verdict in ``data``."""
    data = {
        "code": error.code,
        "message": error.message,
        **failure_fields(error.facts, error.display_message, error.display_hint),
        **extra,
    }
    return RequestError(
        INTERNAL_ERROR_CODE, summary(error.message) or json_text(error.code) or _INTERNAL_ERROR, to_json_object(data)
    )
