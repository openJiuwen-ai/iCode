# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""The Chat Completions request one chat call sends."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from openai.lib._parsing._completions import type_to_response_format_param

from chrys.kernel import (
    FunctionTool,
    Message,
    ToolTypes,
    normalize_tools,
    prepend_instructions_to_messages,
    validate_tool_mode,
)
from chrys.kernel.exceptions import ChatClientInvalidRequestException
from chrys.service.llm._structured_outputs import (
    _materialize_json_structure,
    _sanitize_response_format_name,
    _strictify_response_schema,
)
from chrys.service.llm.output_cap import fold_output_cap_options
from chrys.service.profiles.models.options import STREAM_REQUIRES_FINISH_REASON_OPTION

from .history import encode_messages

if TYPE_CHECKING:
    from chrys.foundation.reasoning_origin import ReasoningOrigin

    from .client import ChatCompletionsVariant

logger = logging.getLogger(__name__)

# Options the request carries in another form or not at all.
_NOT_FORWARDED = frozenset({"instructions", "tools", "conversation_id", STREAM_REQUIRES_FINISH_REASON_OPTION})

# Chat option names and the names this API uses for them.
_RENAMED_OPTIONS = (("allow_multiple_tool_calls", "parallel_tool_calls"), ("max_tokens", "max_completion_tokens"))


def build_request(
    messages: Sequence[Message],
    options: Mapping[str, Any],
    *,
    model: str,
    variant: ChatCompletionsVariant,
    origin: ReasoningOrigin | None = None,
) -> dict[str, Any]:
    """The keyword arguments of one ``chat.completions.create`` call to the endpoint *origin*, before the headers are stamped."""
    options = fold_output_cap_options(options)
    _require_single_choice(options)
    if instructions := options.get("instructions"):
        messages = prepend_instructions_to_messages(list(messages), instructions, role="system")
    request = {key: value for key, value in options.items() if value is not None and key not in _NOT_FORWARDED}
    # Tools first: whether reasoning replays can depend on the request sending any.
    tools = options.get("tools")
    tool_fields = encode_tools(tools) if tools is not None else {}
    if messages and "messages" not in request:
        request["messages"] = encode_messages(
            messages, variant=variant, request_has_tools=bool(tool_fields.get("tools")), origin=origin
        )
    if "messages" not in request:
        raise ChatClientInvalidRequestException("Messages are required for chat completions")
    for chat_name, wire_name in _RENAMED_OPTIONS:
        if chat_name in request:
            request[wire_name] = request.pop(chat_name)
    # Real OpenAI rejects the legacy ``max_tokens`` on current models, while
    # some compatible endpoints (DeepSeek, GLM) honor only that name.
    cap = variant.max_output_param
    if cap != "max_completion_tokens" and cap not in request and "max_completion_tokens" in request:
        request[cap] = request.pop("max_completion_tokens")
    if not request.get("model"):
        if not model:
            raise ValueError("model must be a non-empty string")
        request["model"] = model
    request.update(tool_fields)
    # Tool choice and parallel calls go out only with tools.
    tool_choice = request.pop("tool_choice", None)
    if not request.get("tools"):
        request.pop("parallel_tool_calls", None)
    elif tool_choice and (wire_choice := _wire_tool_choice(tool_choice)) is not None:
        request["tool_choice"] = wire_choice
    # Presence, not truthiness: an empty mapping is a valid JSON schema
    # without a type and still needs its envelope.
    if (response_format := options.get("response_format")) is not None:
        request["response_format"] = encode_response_format(response_format, model=request.get("model"))
    return request


def encode_tools(
    tools: ToolTypes | Callable[..., Any] | Sequence[ToolTypes | Callable[..., Any]] | None,
) -> dict[str, Any]:
    """The request's ``tools`` and ``web_search_options``.

    A function tool goes out as its JSON schema spec. A ``web_search``
    mapping configures the search options rather than joining the list;
    anything else is sent as given.
    """
    listed: list[Any] = []
    search: dict[str, Any] | None = None
    for tool in normalize_tools(tools):
        if isinstance(tool, FunctionTool):
            listed.append(tool.to_json_schema_spec())
            continue
        if isinstance(tool, MutableMapping):
            spec = cast("MutableMapping[str, Any]", tool)
            if spec.get("type") == "web_search":
                search = {key: value for key, value in spec.items() if key != "type"}
                continue
        listed.append(tool)
    fields: dict[str, Any] = {"tools": listed} if listed else {}
    if search is not None:
        fields["web_search_options"] = search
    return fields


def encode_response_format(response_format: Any, *, model: Any) -> dict[str, Any]:
    """The ``response_format`` for a model class, a format envelope or a raw JSON schema.

    The two clients accept the same inputs. An envelope keeps its shape with
    its name made valid; ``json_object`` and ``text`` pass as given. Any
    other mapping is a raw schema: JSON Schema allows an array-valued or an
    absent ``type`` (enum-only schemas), so no keyword test could tell them
    apart.
    """
    if not isinstance(response_format, Mapping):
        # The SDK helper copies the model class's name into the envelope unchecked.
        return _valid_name(cast("dict[str, Any]", type_to_response_format_param(response_format)))
    # Read-only views are copied once; plain dicts pass through by identity.
    given = response_format if isinstance(response_format, dict) else dict(response_format)
    given = cast("dict[str, Any]", given)
    kind = given.get("type")
    if kind == "json_schema":
        return _valid_name(given)
    # An equality test: an array-valued (unhashable) type reads as a raw schema.
    if kind in ("json_object", "text"):
        return given
    # A copy: the strictifier edits nested mappings in place, and the caller's
    # may be read-only views.
    schema = _materialize_json_structure(given)
    # Strict mode rejects unknown keys, so the title becomes the name.
    envelope: dict[str, Any] = {"name": _sanitize_response_format_name(schema.pop("title", None))}
    envelope["schema"], strict = _strictify_response_schema(schema, model=model)
    if strict:
        envelope["strict"] = True
    return {"type": "json_schema", "json_schema": envelope}


def _valid_name(response_format: dict[str, Any]) -> dict[str, Any]:
    """A ``json_schema`` envelope with a valid name; copied only when the name changes."""
    json_schema = response_format.get("json_schema")
    if not isinstance(json_schema, Mapping):
        return response_format
    schema_spec = cast("Mapping[str, Any]", json_schema)
    name = schema_spec.get("name")
    valid = _sanitize_response_format_name(name)
    if valid == name:
        return response_format
    return {**response_format, "json_schema": {**schema_spec, "name": valid}}


def _wire_tool_choice(tool_choice: Any) -> Any:
    """The ``tool_choice`` value for a chat tool mode; ``None`` when there is none."""
    mode = validate_tool_mode(tool_choice)
    if mode is None:
        return None
    name = mode.get("mode")
    if name == "required" and (function := mode.get("required_function_name")) is not None:
        return {"type": "function", "function": {"name": function}}
    if name in ("auto", "required") and mode.get("allowed_tools") is not None:
        logger.warning(
            "The Chat Completions client does not send allowed_tools; the setting is ignored "
            "(the Responses API client sends it)."
        )
    return name


def _require_single_choice(options: Mapping[str, Any]) -> None:
    """Refuse ``n`` other than 1: the kernel reads one conversation, not alternative choices.

    ``extra_body`` counts too, as the SDK merges it over the named parameters.
    """
    extra_body = options.get("extra_body")
    for source in (options, extra_body if isinstance(extra_body, Mapping) else {}):
        count = cast("Mapping[str, Any]", source).get("n")
        if count is not None and (type(count) is not int or count != 1):
            raise ChatClientInvalidRequestException("The Chat Completions client supports only n=1")
