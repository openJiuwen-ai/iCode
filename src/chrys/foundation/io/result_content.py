# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Normalize rich tool results into display text and image content."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from chrys.foundation.text.images import is_image_media_type
from chrys.foundation.text.model_json import model_json

logger = logging.getLogger(__name__)


# Marker prefix used by chrys to tag tool results as failures.  TUI
# renderers check ``result.startswith(_ERROR_PREFIX)`` to apply error
# styling, and the LLM treats it as an error signal.
_ERROR_PREFIX = "Error: "


def _render_attr_value(val: Any) -> str:
    """Render a Content text-bearing attribute value to a string.

    The text-bearing slots (``text``/``result``/``output``/``message``)
    are typed ``Any`` on ``Content`` and in practice can hold non-string
    values:

    * ``Content.from_mcp_server_tool_result(output=...)`` accepts a
      ``list[Content]`` (the Anthropic provider builds these for
      ``mcp_tool_result`` blocks from Anthropic-managed MCP servers).
    * Custom tools / providers may set ``result`` or ``output`` to a
      ``dict`` (e.g. structured JSON returned from an MCP server) or
      another nested container.

    Naively calling ``str(val)`` on these renders unusable object reprs
    because ``Content`` does not override ``__repr__``.  Instead:

    * ``list``/``tuple`` -> recurse into :func:`extract_result_text` so
      Content items inside the list are properly unwrapped.
    * ``Mapping`` -> JSON-dump (preserves ``{"code": 500, "body": ...}``
      style structured error payloads as readable JSON).
    * everything else -> ``str(val)`` (numbers, bools, plain objects).
    """
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)):
        return extract_result_text(list(val))
    if isinstance(val, Mapping):
        try:
            return model_json(val, default=str)
        except TypeError, ValueError:
            return str(val)
    return str(val)


def _content_to_text(item: Any) -> str:
    """Render a single ``Content``-like item to a string.

    Tries the known text-bearing attributes in order (``text`` for text
    items, ``result`` for function_result, ``output`` for
    mcp_server_tool_result, ``message`` for error).  For data/uri items
    (images, audio, embedded blobs) returns a ``[<media_type>: <uri...>]``
    placeholder so the LLM still sees something meaningful instead of a
    bare object repr.

    If the item has none of those attributes (e.g. an unexpected
    function_call / approval_request Content showing up where a result
    was expected), logs a WARNING and returns an ``"Error: ..."`` string
    so the failure is surfaced both in logs and to the LLM/TUI rather
    than masquerading as a successful structured result.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        return _render_attr_value(item)
    # The first text-bearing attribute that is *present and non-None* wins,
    # even if its value is the empty string: an empty text item should
    # render as ``""`` (filtered upstream), not fall through to the
    # structural-fields dump and emit ``{"type": "text", ...}``.
    for attr in ("text", "result", "output", "message"):
        val = getattr(item, attr, None)
        if val is None:
            continue
        return _render_attr_value(val)
    uri = getattr(item, "uri", None)
    if uri:
        media_type = getattr(item, "media_type", None) or "binary"
        if _is_image_data_uri(media_type, uri):
            return f"[{media_type} image]"
        snippet = uri if len(uri) <= 120 else uri[:117] + "..."
        return f"[{media_type}: {snippet}]"
    # Unrecognized shape: every documented success path produces a
    # Content with at least one of text/result/output/message/uri.
    # Hitting this branch means either a framework Content schema drift
    # or a misrouted call/state container ending up in a result list.
    # Treat it as an error: log a warning and emit a clearly tagged
    # diagnostic string so the LLM doesn't read raw JSON as success.
    if hasattr(item, "__dict__"):
        try:
            d = {k: v for k, v in vars(item).items() if v not in (None, "") and not k.startswith("_")}
        except TypeError:
            d = {}
        if not d:
            # Object truly carries nothing: render as empty (filtered upstream).
            return ""
        type_hint = d.get("type", type(item).__name__)
        logger.warning(
            "Unrecognized tool result Content (type=%r); rendering as fallback. Fields: %s",
            type_hint,
            {k: _summarize_value(v) for k, v in d.items()},
        )
        return (
            f"{_ERROR_PREFIX}unrecognized tool result content (type={type_hint}); fields: {model_json(d, default=str)}"
        )
    return str(item)


def extract_result_text(result: Any) -> str:
    """Extract displayable text from a tool invocation result.

    Since rc5, ``FunctionTool.invoke()`` returns ``list[Content]`` instead
    of a plain string.  This walks the list and extracts text from each
    Content item via :func:`_content_to_text`.  When a Content item is
    not understood, an unstructured fallback is logged at DEBUG so the
    raw shape is recoverable from logs without crashing the tool result.
    """
    if result is None:
        return ""
    # Guard against dangling coroutines left by cancelled tool invocations.
    # FunctionMiddlewarePipeline can leave context.result holding the handler
    # coroutine if cancellation interrupts its await. Close it rather than
    # rendering a coroutine object as the tool's result.
    import inspect

    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if close is not None:
            close()
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Tool result list: %d item(s); types=%s; shapes=%s",
                len(result),
                [getattr(item, "type", type(item).__name__) for item in result],
                [_summarize_item_shape(item) for item in result],
            )
        rendered = [_content_to_text(item) for item in result]
        parts = [p for p in rendered if p]
        if not parts:
            return ""
        # If any item triggered the unrecognized-content fallback, the
        # whole result must surface as an error so the TUI styling and
        # the chrys "Error: ..." convention apply to the joined string.
        any_error = any(p.startswith(_ERROR_PREFIX) for p in parts)
        joined = "\n".join(parts)
        if any_error and not joined.startswith(_ERROR_PREFIX):
            joined = _ERROR_PREFIX + joined
        return joined
    return _content_to_text(result)


def extract_result_images(result: Any) -> list[Any]:
    """Extract image Content-like items from a tool invocation result."""
    images: list[Any] = []
    for item in _media_items(result):
        media_type = getattr(item, "media_type", None)
        properties = getattr(item, "additional_properties", None)
        property_media_type = properties.get("media_type") if isinstance(properties, Mapping) else None
        if is_image_media_type(media_type) or is_image_media_type(property_media_type):
            images.append(item)
    return images


def extract_result_attachments(result: Any) -> list[Any]:
    """Extract the media Content-like items of a tool invocation result: images, audio, files.

    What a text replacement has to keep. A hook hands back text only, so a
    result carrying an image, an audio clip or a file beside its text would
    lose them to that text alone; the model would no longer see what the tool
    gave it. :func:`extract_result_images` is the images of these.
    """
    return _media_items(result)


_ITEMS_SLOTS = ("function_result", "hosted_tool_result", "search_tool_result")
"""Result contents that carry their nested contents in ``items``."""

_OUTPUTS_SLOTS = ("shell_tool_result", "code_interpreter_tool_result", "image_generation_tool_result")
"""Result contents that carry their nested contents in ``outputs``."""

_OUTPUT_SLOT = "mcp_server_tool_result"
"""The result content that carries its nested contents in ``output``."""


def _media_items(result: Any) -> list[Any]:
    """The ``data``/``uri`` items of a result, beside its text, wherever its kind nests them."""
    if result is None or isinstance(result, str):
        return []
    if isinstance(result, list):
        items: list[Any] = []
        for item in result:
            items.extend(_media_items(item))
        return items
    item_type = getattr(result, "type", None)
    if item_type in ("data", "uri"):
        return [result]
    # Each kind of result content keeps what it holds in its own slot, so the kind
    # decides which one to walk rather than trying all three.
    if item_type in _ITEMS_SLOTS:
        return _media_items(getattr(result, "items", None))
    if item_type in _OUTPUTS_SLOTS:
        return _media_items(getattr(result, "outputs", None))
    if item_type == _OUTPUT_SLOT:
        return _media_items(getattr(result, "output", None))
    return []


def _is_image_data_uri(media_type: Any, uri: Any) -> bool:
    return (
        isinstance(media_type, str)
        and media_type.lower().startswith("image/")
        and isinstance(uri, str)
        and uri.startswith("data:")
    )


_DEBUG_STR_PREVIEW_CHARS = 80


def _summarize_value(v: Any) -> Any:
    """Render a single value as a debug-log-safe summary.

    Long strings become ``<str len=N preview='...'>`` (only the first
    ``_DEBUG_STR_PREVIEW_CHARS`` chars previewed, via ``repr`` so any
    binary/escape sequences stay quoted). Bytes and containers become
    ``<typename len=N>`` (no contents). Numbers/bools pass through.
    Other objects become ``<typename>``.
    """
    if isinstance(v, str):
        if len(v) <= _DEBUG_STR_PREVIEW_CHARS:
            return v
        return f"<str len={len(v)} preview={v[:_DEBUG_STR_PREVIEW_CHARS]!r}>"
    if isinstance(v, (bytes, bytearray, list, tuple, dict, set)):
        return f"<{type(v).__name__} len={len(v)}>"
    if isinstance(v, (int, float, bool)):
        return v
    return f"<{type(v).__name__}>"


def _summarize_item_shape(item: Any) -> Any:
    """Render a result-list item as a debug-log-safe shape descriptor.

    For objects with ``__dict__`` (e.g. ``Content``):
    returns a dict mapping field name -> :func:`_summarize_value` of the
    value.  For plain values (``str``, ``bytes``, primitives, etc.):
    returns the same summary directly.  Either way, large payloads:
    base64 ``data:`` URIs, blob contents, big strings: never appear
    verbatim in the DEBUG log.
    """
    if not hasattr(item, "__dict__"):
        return _summarize_value(item)
    try:
        fields = {k: v for k, v in vars(item).items() if v not in (None, "") and not k.startswith("_")}
    except TypeError:
        return _summarize_value(item)
    return {k: _summarize_value(v) for k, v in fields.items()}
