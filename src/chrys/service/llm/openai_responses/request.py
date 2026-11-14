# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Chat options and history as a Responses create request.

:func:`build_request` copies the options the endpoint understands, renames
the chat names it spells differently, and adds the input (:mod:`.replay`),
tools, tool choice and the ``text`` configuration a response format becomes.
A stateless variant first drops every stored-response handle.
:func:`set_prompt_cache_key` then picks the key that routes the prompt cache.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, MutableMapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from openai.types.responses.function_tool_param import FunctionToolParam
from pydantic import BaseModel

from chrys.kernel import FunctionTool, normalize_tools, validate_tool_mode
from chrys.kernel.exceptions import ChatClientInvalidRequestException
from chrys.service.llm._structured_outputs import (
    _materialize_json_structure,
    _sanitize_response_format_name,
    _strictify_response_schema,
)
from chrys.service.llm.output_cap import fold_output_cap_options
from chrys.service.profiles.models.options import PROMPT_CACHE_KEY_OPTION

from .replay import encode_input

if TYPE_CHECKING:
    from chrys.foundation.reasoning_origin import ReasoningOrigin
    from chrys.kernel import Message

    from .client import ResponsesVariant

# Chat options a request leaves out: unsupported by the endpoint, or turned
# into request fields below. ``instructions`` is copied, so changed
# instructions take effect on requests that continue a stored response too.
_NOT_COPIED = frozenset(
    {
        "type",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "seed",
        "stop",
        "response_format",
        "conversation_id",
        "tool_choice",
        "continuation_token",
    }
)
_RENAMED = (("allow_multiple_tool_calls", "parallel_tool_calls"), ("max_tokens", "max_output_tokens"))
_STORED_RESPONSE_HANDLES = ("conversation_id", "previous_response_id", "conversation")
_ENCRYPTED_REASONING = "reasoning.encrypted_content"
_PROMPT_CACHE_KEY_MAX_CHARS = 64


def build_request(
    messages: Sequence[Message],
    options: Mapping[str, Any],
    *,
    model: str,
    variant: ResponsesVariant,
    origin: ReasoningOrigin | None = None,
) -> dict[str, Any]:
    """The create request for *messages* under validated chat *options*, sent to the endpoint *origin*."""
    options = fold_output_cap_options(options)
    if variant.stateless:
        options = stateless_view(options)
    service_side = continues_stored_response(options)
    request: dict[str, Any] = {
        key: value for key, value in options.items() if key not in _NOT_COPIED and value is not None
    }
    # Encrypted reasoning is what local history replays, so it is asked for
    # unless the service holds the history. An explicit empty ``include``
    # opts out (some gateways reject the value); a non-empty one still gains it.
    caller_include = options.get("include")
    opted_out = isinstance(caller_include, list) and not caller_include
    if variant.encrypted_reasoning and not service_side and not opted_out:
        include = list(request.get("include", []))
        if _ENCRYPTED_REASONING not in include:
            include.append(_ENCRYPTED_REASONING)
        request["include"] = include
    if not request.get("include"):
        request.pop("include", None)

    request_input = encode_input(messages, service_side=service_side, variant=variant, origin=origin)
    if not request_input:
        raise ChatClientInvalidRequestException("Messages are required for chat completions")
    request["input"] = request_input
    if not request.get("model"):
        if not model:
            raise ValueError("model must be a non-empty string")
        request["model"] = model
    for name, wire_name in _RENAMED:
        if name in request:
            request[wire_name] = request.pop(name)
    if conversation_id := options.get("conversation_id"):
        handle = "conversation" if conversation_id.startswith("conv_") else "previous_response_id"
        request[handle] = conversation_id

    if tools := encode_tools(options.get("tools")):
        request["tools"] = tools
        if (choice := options.get("tool_choice")) and (mode := validate_tool_mode(choice)) is not None:
            request["tool_choice"] = _tool_choice(mode)
    else:
        request.pop("parallel_tool_calls", None)
        request.pop("tool_choice", None)

    parse_target, text = _text_options(
        options.get("response_format"), request.pop("text", None), model=request.get("model")
    )
    # Verbosity is a top-level option, like ``reasoning``; the endpoint nests it.
    if (verbosity := request.pop("verbosity", None)) is not None:
        text = dict(text) if text else {}
        text["verbosity"] = verbosity
    if text:
        request["text"] = _named_schema_format(text)
    if parse_target:
        request["text_format"] = _named_parse_target(parse_target)
    return request


def set_prompt_cache_key(request: dict[str, Any], options: Mapping[str, Any], *, session_id: str | None) -> None:
    """Route the request's prompt cache by *session_id* unless the options set the key.

    A key the options set goes out as written, from ``extra_body`` before
    the top level; a null in either place sends no key at all. A session id
    longer than the API takes is sent as its SHA-256 hex digest.
    """
    extra_body = options.get("extra_body")
    nested = extra_body if isinstance(extra_body, Mapping) else {}
    if (PROMPT_CACHE_KEY_OPTION in nested and nested[PROMPT_CACHE_KEY_OPTION] is None) or (
        PROMPT_CACHE_KEY_OPTION in options and options[PROMPT_CACHE_KEY_OPTION] is None
    ):
        request.pop(PROMPT_CACHE_KEY_OPTION, None)
        sent = request.get("extra_body")
        if isinstance(sent, Mapping) and PROMPT_CACHE_KEY_OPTION in sent:
            request["extra_body"] = {name: value for name, value in sent.items() if name != PROMPT_CACHE_KEY_OPTION}
        return
    if PROMPT_CACHE_KEY_OPTION in nested or options.get(PROMPT_CACHE_KEY_OPTION) is not None or not session_id:
        return
    if len(session_id) > _PROMPT_CACHE_KEY_MAX_CHARS:
        session_id = hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()
    request[PROMPT_CACHE_KEY_OPTION] = session_id


def continues_stored_response(options: Mapping[str, Any]) -> bool:
    """Whether the request continues a response the service stored, and so holds the history before it."""
    for key in _STORED_RESPONSE_HANDLES:
        value = options.get(key)
        handle = value.get("id") if isinstance(value, Mapping) else value
        if isinstance(handle, str) and handle:
            return True
    return False


def stateless_view(options: Mapping[str, Any]) -> dict[str, Any]:
    """*options* without stored-response handles, for an endpoint that stores nothing.

    A requested ``store`` becomes an explicit false, so storage reads as
    off rather than as the endpoint default.
    """
    stateless = dict(options)
    extra_body = stateless.get("extra_body")
    clean_extra_body = dict(extra_body) if isinstance(extra_body, Mapping) else None
    had_store = "store" in stateless or (clean_extra_body is not None and "store" in clean_extra_body)
    for key in ("conversation_id", "previous_response_id", "conversation", "continuation_token"):
        stateless.pop(key, None)
        if clean_extra_body is not None:
            clean_extra_body.pop(key, None)
    if had_store:
        stateless["store"] = False
        if clean_extra_body is not None:
            clean_extra_body.pop("store", None)
    if clean_extra_body is not None:
        stateless["extra_body"] = clean_extra_body
    return stateless


def reject_stateful_options(options: Mapping[str, Any]) -> None:
    """Refuse options a stateless endpoint cannot honor: resuming or backgrounding a response."""
    extra_body = options.get("extra_body")
    nested = extra_body if isinstance(extra_body, Mapping) else {}
    if options.get("continuation_token") is not None or nested.get("continuation_token") is not None:
        raise ChatClientInvalidRequestException("DeepSeek Responses does not support continuation_token")
    if options.get("background") is not None or nested.get("background") is not None:
        raise ChatClientInvalidRequestException(
            "DeepSeek Responses does not support background responses because the dialect is stateless"
        )


def encode_tools(tools: Any) -> list[Any]:
    """Function tools as Responses function tools; anything else (dicts, SDK types) as given."""
    encoded: list[Any] = []
    for tool in normalize_tools(tools):
        if not isinstance(tool, FunctionTool):
            encoded.append(tool)
            continue
        # A copy: ``parameters()`` is the cached schema local validation reads.
        parameters = dict(tool.parameters())
        parameters["additionalProperties"] = False
        encoded.append(
            FunctionToolParam(
                name=tool.name, parameters=parameters, strict=False, type="function", description=tool.description
            )
        )
    return encoded


def _tool_choice(mode: Mapping[str, Any]) -> Any:
    """A validated tool mode as the endpoint's ``tool_choice``."""
    kind = mode.get("mode")
    if kind == "required" and (function_name := mode.get("required_function_name")) is not None:
        return {"type": "function", "name": function_name}
    if kind in ("auto", "required") and (allowed := mode.get("allowed_tools")) is not None:
        return {
            "type": "allowed_tools",
            "mode": kind,
            "tools": [{"type": "function", "name": name} for name in allowed],
        }
    return kind


def _text_options(
    response_format: Any, text: Any, *, model: Any
) -> tuple[type[BaseModel] | None, MutableMapping[str, Any] | None]:
    """The model the SDK parses into, and the ``text`` configuration, for a response format.

    A Pydantic model is handed to the SDK as is; a mapping becomes
    ``text.format``, which an explicit ``text.format`` may only repeat.
    """
    if text is not None and not isinstance(text, MutableMapping):
        raise ChatClientInvalidRequestException("text must be a mapping when provided.")
    text = cast("MutableMapping[str, Any] | None", text)
    if response_format is None:
        return None, text
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        if text and "format" in text:
            raise ChatClientInvalidRequestException("response_format cannot be combined with explicit text.format.")
        return response_format, text
    if not isinstance(response_format, Mapping):
        raise ChatClientInvalidRequestException("response_format must be a Pydantic model or mapping.")
    text_format = _text_format(cast("Mapping[str, Any]", response_format), model=model)
    if text is None:
        return None, {"format": text_format}
    if "format" in text and text["format"] != text_format:
        raise ChatClientInvalidRequestException("Conflicting response_format definitions detected.")
    # A copy: *text* is the profile's option object, which later requests reuse.
    return None, {**text, "format": text_format}


def _text_format(response_format: Mapping[str, Any], *, model: Any) -> dict[str, Any]:
    """A chat ``response_format`` as a Responses ``text.format``."""
    if "format" in response_format and isinstance(response_format["format"], Mapping):
        return dict(cast("Mapping[str, Any]", response_format["format"]))
    kind = response_format.get("type")
    if kind == "json_schema":
        return _wrapped_schema(response_format.get("json_schema", response_format))
    # Tuple membership compares by equality, so an array-valued ``type``
    # (unhashable) reads as a bare schema instead of raising.
    if kind in ("json_object", "text"):
        return {"type": kind}
    return _bare_schema(response_format, model=model)


def _wrapped_schema(section: Any) -> dict[str, Any]:
    """A ``json_schema`` response format: its schema, name, strictness and description."""
    if not isinstance(section, Mapping):
        raise ChatClientInvalidRequestException("json_schema response_format must be a mapping.")
    section = cast("Mapping[str, Any]", section)
    schema = section.get("schema")
    if schema is None:
        raise ChatClientInvalidRequestException("json_schema response_format requires a schema.")
    name = str(
        section.get("name")
        or section.get("title")
        or (cast("Mapping[str, Any]", schema).get("title") if isinstance(schema, Mapping) else None)
        or "response"
    )
    text_format: dict[str, Any] = {"type": "json_schema", "name": name, "schema": schema}
    if "strict" in section:
        text_format["strict"] = section["strict"]
    if section.get("description") is not None:
        text_format["description"] = section["description"]
    return text_format


def _bare_schema(response_format: Mapping[str, Any], *, model: Any) -> dict[str, Any]:
    """A bare JSON Schema, wrapped and made strict where the model allows.

    Any other mapping counts as one: JSON Schema allows an array-valued or
    absent ``type`` (enum-only schemas), so sniffing for schema keywords
    would miss some.
    """
    # A deep, mutable copy: the mapping may hold read-only views at any
    # depth, and making the schema strict edits nested mappings in place.
    schema = _materialize_json_structure(response_format)
    # Strict mode rejects unknown keys, so the title becomes the name.
    name = str(schema.pop("title", None) or "response")
    schema, strict = _strictify_response_schema(schema, model=model)
    text_format: dict[str, Any] = {"type": "json_schema", "name": name, "schema": schema}
    if strict:
        text_format["strict"] = True
    return text_format


def _named_schema_format(text: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """*text* with a ``json_schema`` format name the endpoint accepts.

    Every way a format name is produced ends here, a caller's own ``text``
    included. Copies only when the name changes, so other formats go out
    exactly as given.
    """
    text_format = text.get("format")
    if not isinstance(text_format, Mapping):
        return text
    text_format = cast("Mapping[str, Any]", text_format)
    if text_format.get("type") != "json_schema":
        return text
    name = text_format.get("name")
    valid = _sanitize_response_format_name(name)
    if name == valid:
        return text
    return {**text, "format": {**text_format, "name": valid}}


def _named_parse_target(model: type[BaseModel]) -> type[BaseModel]:
    """*model*, or a subclass whose name the endpoint accepts as the format name.

    The SDK names the format after the class, and a Unicode or over-long
    class name is legal Python. The subclass adds nothing, so every parsed
    value is still an instance of *model*.
    """
    valid = _sanitize_response_format_name(model.__name__)
    if valid == model.__name__:
        return model
    return cast("type[BaseModel]", type(valid, (model,), {}))
