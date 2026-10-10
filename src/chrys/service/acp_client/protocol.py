# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP protocol validation and retained payload caps."""

from __future__ import annotations

import base64
import binascii
import json
import math
from typing import Any, Literal

from chrys.foundation.text.images import MAX_IMAGE_BYTES
from chrys.foundation.util.unicode_scalars import find_unpaired_surrogate

_MAX_PAYLOAD_BYTES = 1024 * 1024
# A request can carry a whole file the agent is about to write, and a diff of
# it. Re-encoded for the SDK reader with every non-ASCII character escaped, a
# request at this bound grows at most threefold, still under the stdio limit.
_MAX_REQUEST_PAYLOAD_BYTES = 8 * 1024 * 1024
_MAX_PAYLOAD_DEPTH = 32
_MAX_COLLECTION_ITEMS = 4_096
# An agent's requests and notifications describe its tool calls, and one patch
# over a few hundred files carries a diff, a location and an argument entry for
# each: past 4,096 items in all. They keep the per-collection cap under this
# larger total, which still bounds what validation and the translator walk in a
# frame of tiny values.
_MAX_AGENT_MESSAGE_ITEMS = 64 * 1024
_MAX_STRING_CHARS = 256 * 1024
_MAX_BASE64_IMAGE_CHARS = ((MAX_IMAGE_BYTES + 2) // 3) * 4
_MAX_REQUEST_ID_CHARS = 4_096
_MAX_METHOD_CHARS = 1_024
# An update's ids are kept whole (map keys, local call ids) and repeated in
# every event about the call, so they keep a cap when the text around them
# has none.
_MAX_UPDATE_ID_CHARS = 4_096
_UPDATE_ID_FIELDS = (("toolCallId", "tool_call_id"), ("messageId", "message_id"))
_TOOL_UPDATE_KINDS = ("tool_call", "tool_call_update")

_PERMISSION_METHOD = "session/request_permission"
_ASK_USER_METHOD = "_chrys/request_input"


def validate_json_scalar_tree(value: Any) -> None:
    """Validate every JSON key, value, and sequence element recursively."""
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite.")
        return
    if type(value) is str:
        _validate_surrogates(value)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings.")
            _validate_surrogates(key)
            validate_json_scalar_tree(item)
        return
    if type(value) in {list, tuple}:
        for item in value:
            validate_json_scalar_tree(item)
        return
    raise ValueError(f"Unsupported JSON value type: {type(value).__name__}")


def _validate_surrogates(value: str) -> None:
    if find_unpaired_surrogate(value) >= 0:
        raise ValueError("JSON strings cannot contain unpaired surrogates.")


def encode_protocol_json(value: Any) -> bytes:
    """Encode a protocol-bound value after the shared scalar validation pass."""
    validate_json_scalar_tree(value)
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def sdk_field_values(value: dict[str, Any], alias: str, name: str) -> list[Any]:
    """Return what *value* holds under each spelling of one SDK model field.

    The SDK's models are built with ``populate_by_name``: they read a field by
    its alias or by its Python name, and when both are present a field takes
    the alias while a tagged union picks its branch by the name. A check on
    the raw frame therefore runs on every spelling present.
    """
    return [value[key] for key in (alias, name) if key in value]


def protocol_json_size(value: Any) -> int:
    """Return the size of *value* as compact UTF-8 JSON, the bytes an agent's frame spends on it.

    :func:`encode_protocol_json` escapes every non-ASCII character, up to three
    times what a frame carries, so a bound measured with it would refuse a
    frame the stdio limit admits.
    """
    validate_json_scalar_tree(value)
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))


def parse_protocol_json(raw: bytes) -> Any:
    """Parse one JSON value while rejecting JavaScript non-finite constants."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"Invalid JSON numeric constant: {value}")

    parsed = json.loads(raw, parse_constant=reject_constant)
    validate_json_scalar_tree(parsed)
    return parsed


_ENVELOPE_MEMBERS = frozenset({"jsonrpc", "id", "method", "params", "result", "error"})


def validate_json_rpc_envelope(message: Any) -> Literal["request", "notification", "response"]:
    """Validate the strict JSON-RPC envelope accepted by the SDK-facing reader."""
    if type(message) is not dict:
        raise ValueError("JSON-RPC frame root must be an object.")
    # The per-kind caps run on params/result only, and the SDK re-parses the
    # complete frame; an unknown top-level member would ride around both.
    if not _ENVELOPE_MEMBERS.issuperset(message):
        raise ValueError("JSON-RPC frame contains unknown envelope members.")
    if message.get("jsonrpc") != "2.0" or type(message.get("jsonrpc")) is not str:
        raise ValueError("JSON-RPC frame must declare version 2.0.")

    has_id = "id" in message
    has_method = "method" in message
    has_result = "result" in message
    has_error = "error" in message

    if has_id and type(message["id"]) not in {str, int}:
        raise ValueError("JSON-RPC ids must be exact strings or integers.")
    # Ids and methods are retained (inbound map keys, outgoing records) and
    # echoed into response frames, so they carry their own §7.4-style caps.
    if has_id and type(message["id"]) is str and len(message["id"]) > _MAX_REQUEST_ID_CHARS:
        raise ValueError("JSON-RPC id exceeds the retained-field cap.")
    if has_id and type(message["id"]) is int and not -(1 << 63) <= message["id"] < 1 << 63:
        raise ValueError("JSON-RPC id exceeds the interoperable integer range.")
    if has_method and type(message["method"]) is not str:
        raise ValueError("JSON-RPC methods must be strings.")
    if has_method and len(message["method"]) > _MAX_METHOD_CHARS:
        raise ValueError("JSON-RPC method exceeds the retained-field cap.")
    if "params" in message:
        if not has_method:
            raise ValueError("JSON-RPC responses cannot contain params.")
        if type(message["params"]) is not dict:
            raise ValueError("JSON-RPC params must be an object.")

    if has_method:
        if has_result or has_error:
            raise ValueError("JSON-RPC requests cannot contain result or error.")
        return "request" if has_id else "notification"

    if not has_id or has_result == has_error:
        raise ValueError("JSON-RPC responses require an id and exactly one of result or error.")
    if has_error:
        error = message["error"]
        if type(error) is not dict:
            raise ValueError("JSON-RPC error must be an object.")
        if type(error.get("code")) is not int or type(error.get("message")) is not str:
            raise ValueError("JSON-RPC error requires an exact integer code and string message.")
    return "response"


def _count_payload_items(value: Any, *, max_string_chars: int | None, depth: int = 0) -> int:
    if depth > _MAX_PAYLOAD_DEPTH:
        raise ValueError("ACP payload nesting is too deep.")
    if type(value) is str:
        if max_string_chars is not None and len(value) > max_string_chars:
            raise ValueError("ACP payload string is too large.")
        return 1
    if type(value) is dict:
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise ValueError("ACP payload object has too many fields.")
        items = 1
        for key, item in value.items():
            if max_string_chars is not None and len(key) > max_string_chars:
                raise ValueError("ACP payload string is too large.")
            items += _count_payload_items(item, max_string_chars=max_string_chars, depth=depth + 1)
        return items
    if type(value) in {list, tuple}:
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise ValueError("ACP payload sequence has too many items.")
        items = 1
        for item in value:
            items += _count_payload_items(item, max_string_chars=max_string_chars, depth=depth + 1)
        return items
    return 1


def _validate_payload_shape(value: Any, *, max_string_chars: int | None, max_total_items: int) -> None:
    # Per-collection checks alone admit 4096-wide children at every level;
    # the aggregate bound is what actually caps the object count.
    if _count_payload_items(value, max_string_chars=max_string_chars) > max_total_items:
        raise ValueError("ACP payload has too many items in total.")


def _validate_payload_caps(value: Any) -> None:
    """Caps for responses, which are retained whole until their request settles."""
    _validate_payload_shape(value, max_string_chars=_MAX_STRING_CHARS, max_total_items=_MAX_COLLECTION_ITEMS)
    if len(encode_protocol_json(value)) > _MAX_PAYLOAD_BYTES:
        raise ValueError("ACP retained payload exceeds the byte limit.")


def _validate_request_payload_caps(value: Any) -> None:
    """Caps for requests from the agent: permission, ask-user and extension requests.

    Their arguments reach the approval dialog and the judge whole, so a request
    keeps a byte bound, but no string has one of its own: a file the agent
    writes is one long string.
    """
    _validate_payload_shape(value, max_string_chars=None, max_total_items=_MAX_AGENT_MESSAGE_ITEMS)
    if protocol_json_size(value) > _MAX_REQUEST_PAYLOAD_BYTES:
        raise ValueError("ACP request payload exceeds the byte limit.")


def _validate_notification_payload_caps(value: Any) -> None:
    """Caps for notifications: nesting and item count only.

    Those are the shapes that cost far more to parse and walk than their bytes.
    No notification is retained whole: the translator keeps bounded previews of
    tool calls, message text has its own per-attempt budget, and extension
    notifications are dropped. The stdio frame limit and the update buffer's
    byte budget bound their size, so a long string, such as a file a sub-agent
    writes, never ends the transport.
    """
    _validate_payload_shape(value, max_string_chars=None, max_total_items=_MAX_AGENT_MESSAGE_ITEMS)


def _validate_update_image_data(data: str) -> None:
    """Validate one tool image's base64 payload."""
    if not data:
        raise ValueError("ACP image payload is empty.")
    if len(data) > _MAX_BASE64_IMAGE_CHARS:
        raise ValueError("ACP image payload exceeds the supported size limit.")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("ACP image payload is not valid base64.") from exc
    if not decoded:
        raise ValueError("ACP image payload is empty.")
    if len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError("ACP image payload exceeds the supported size limit.")


def _validate_update_tool_images(update: dict[str, Any]) -> None:
    if not any(kind in _TOOL_UPDATE_KINDS for kind in sdk_field_values(update, "sessionUpdate", "session_update")):
        return
    content = update.get("content")
    if type(content) is not list:
        return
    for item in content:
        if type(item) is not dict or item.get("type") != "content":
            continue
        block = item.get("content")
        if type(block) is not dict or block.get("type") != "image":
            continue
        if not any(type(mime) is str for mime in sdk_field_values(block, "mimeType", "mime_type")):
            continue
        data = block.get("data")
        if type(data) is str:
            _validate_update_image_data(data)


def _validate_update_payload_caps(value: Any) -> None:
    """Validate a session update's ids and tool images, then apply the notification caps."""
    update = value.get("update") if type(value) is dict else None
    if type(update) is dict:
        for alias, name in _UPDATE_ID_FIELDS:
            for item in sdk_field_values(update, alias, name):
                if type(item) is str and len(item) > _MAX_UPDATE_ID_CHARS:
                    raise ValueError("ACP update id exceeds the retained-field cap.")
        _validate_update_tool_images(update)
    _validate_notification_payload_caps(value)
