# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Build the ``messages.create`` arguments of one call.

:func:`build_request` decides every request field: the renamed chat options,
the default output cap, the encoded history and system prompt, the thinking
block binding, the ``anthropic-beta`` header, the user id, tool declarations
and structured output. The client stamps its Chrys headers on the result.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from chrys.kernel import (
    ChatClientInvalidRequestException,
    FunctionTool,
    Message,
    normalize_tools,
    prepend_instructions_to_messages,
    validate_tool_mode,
)
from chrys.service.llm.output_cap import fold_output_cap_options
from chrys.service.profiles.models.options import AUTO_INTERLEAVED_THINKING_OPTION, THINKING_BLOCK_BINDING_OPTION

from .history import encode_history
from .thinking_binding import (
    BINDING_CONTROLS_BETA,
    INTERLEAVED_THINKING_BETA,
    ThinkingBindingPolicy,
    apply_thinking_binding,
    resolve_thinking_binding,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from chrys.foundation.reasoning_origin import ReasoningOrigin
    from chrys.kernel import Content

logger = logging.getLogger(__name__)

DEFAULT_BETAS: Final = ("mcp-client-2025-04-04", "code-execution-2025-08-25")
"""Betas every request enables unless its ``extra_headers`` name the betas themselves."""

BETA_HEADER: Final = "anthropic-beta"

FALLBACK_MAX_OUTPUT_TOKENS: Final = 16 * 1024
"""Output cap of a call that sets none: the Messages API requires one.

Profile-driven calls never use it: ``effective_chat_options`` gives every
live request the profile's ``max_output_tokens``, whose default
(``DEFAULT_MAX_OUTPUT_TOKENS`` in ``chrys.service.profiles.models.schema``)
is a deliberately separate constant.
"""

# (chat option, Messages API field) pairs; an explicit field wins over its option.
_RENAMED_OPTIONS: Final = (("stop", "stop_sequences"), ("instructions", "system"))

# Chat options build_request reads itself instead of copying them, and ``stream``,
# which the call site sets.
_OPTIONS_NOT_COPIED: Final = frozenset(
    {
        "instructions",
        "response_format",
        "additional_beta_flags",
        "betas",
        "allow_multiple_tool_calls",
        "stream",
        THINKING_BLOCK_BINDING_OPTION,
        AUTO_INTERLEAVED_THINKING_OPTION,
    }
)

# Call keywords that configure the call and never become request fields. The
# betas and thinking settings are read from the options only.
_CALL_SETTINGS: Final = frozenset(
    {
        "thread",
        "middleware",
        "additional_beta_flags",
        "betas",
        THINKING_BLOCK_BINDING_OPTION,
        AUTO_INTERLEAVED_THINKING_OPTION,
    }
)


@dataclass(frozen=True, slots=True)
class BuiltRequest:
    """The ``messages.create`` arguments of one call and what they replay."""

    request: dict[str, Any]
    policy: ThinkingBindingPolicy
    thinking: tuple[Content, ...]
    """The reasoning contents the request's thinking blocks replay; none when ``extra_body`` sends its own messages."""


def build_request(
    messages: Sequence[Message],
    options: Mapping[str, Any],
    call_kwargs: Mapping[str, Any],
    *,
    model: str,
    base_url: object,
    default_headers: Mapping[str, object],
    origin: ReasoningOrigin | None = None,
    skip_thinking: Collection[Content] = (),
) -> BuiltRequest:
    """Return the request for *messages* under *options*, without Chrys headers, and what it replays.

    Options set to None are left out. Call keywords become request fields too,
    except private (underscore) names and :data:`_CALL_SETTINGS`. *model* is
    used when neither sets one. *base_url* is where the SDK client sends the
    request and *default_headers* the headers it adds to every request.
    *origin* is the endpoint the request goes to: thinking another one issued
    is left out, and so is the thinking of the contents in *skip_thinking*.
    The result's ``thinking`` lists the reasoning contents the request
    replays: passed back as *skip_thinking*, they build the same request
    without that thinking.
    """
    options = fold_output_cap_options(options)
    if instructions := options.get("instructions"):
        messages = prepend_instructions_to_messages(list(messages), instructions, role="system")

    request = {key: value for key, value in options.items() if value is not None and key not in _OPTIONS_NOT_COPIED}
    _rename_options(request)
    call_fields = fold_output_cap_options(
        {key: value for key, value in call_kwargs.items() if not key.startswith("_") and key not in _CALL_SETTINGS}
    )
    _rename_options(call_fields)
    request.update(call_fields)

    if not request.get("model"):
        if not model:
            raise ValueError("model must be a non-empty string")
        request["model"] = model
    if not request.get("max_tokens"):
        request["max_tokens"] = FALLBACK_MAX_OUTPUT_TOKENS
    policy = resolve_thinking_binding(request, options, base_url=base_url)
    apply_thinking_binding(request, policy)
    history = encode_history(messages, origin=origin, skip=skip_thinking)
    request["messages"] = history.messages
    if messages and isinstance(messages[0], Message) and messages[0].role == "system":
        request["system"] = messages[0].text
    request["extra_headers"] = _with_beta_header(request.get("extra_headers"), options, default_headers, policy)
    if user := request.pop("user", None):
        # The Messages API takes the end-user id as ``metadata.user_id``.
        metadata = dict(request.get("metadata") or {})
        if "user_id" not in metadata:
            metadata["user_id"] = user
        request["metadata"] = metadata
    if tool_fields := encode_tools(options):
        request.update(tool_fields)
    if (response_format := options.get("response_format")) is not None:
        request["output_config"] = _output_config_with_format(request.get("output_config"), response_format)
    extra_body = request.get("extra_body")
    sends_own_messages = isinstance(extra_body, Mapping) and "messages" in extra_body
    return BuiltRequest(request, policy, () if sends_own_messages else history.thinking)


def _with_beta_header(
    extra_headers: object,
    options: Mapping[str, Any],
    default_headers: Mapping[str, object],
    policy: ThinkingBindingPolicy,
) -> dict[str, Any]:
    """*extra_headers* with one ``anthropic-beta`` header in place of every spelling of it.

    The header lists :data:`DEFAULT_BETAS`, ``additional_beta_flags``,
    ``betas`` and the client's default ``anthropic-beta`` header, in that
    order; an ``anthropic-beta`` header *extra_headers* names itself replaces
    them all. The betas *policy* needs follow either way. Comma-separated
    values are split, and each beta is listed once. A request with no beta
    gets no header.
    """
    headers = dict(extra_headers) if isinstance(extra_headers, Mapping) else {}
    if any(_is_beta_header(name) and not isinstance(value, str) for name, value in headers.items()):
        # Left as written, so the request's header check refuses the value.
        return headers
    explicit = [headers.pop(name) for name in list(headers) if _is_beta_header(name)]
    sources: list[object] = explicit or [
        DEFAULT_BETAS,
        options.get("additional_beta_flags"),
        options.get("betas"),
        *(value for name, value in default_headers.items() if _is_beta_header(name) and isinstance(value, str)),
    ]
    if policy.controls_beta:
        sources.append(BINDING_CONTROLS_BETA)
    if policy.interleaved_beta:
        sources.append(INTERLEAVED_THINKING_BETA)
    if betas := dict.fromkeys(beta for source in sources for beta in _betas_in(source)):
        headers[BETA_HEADER] = ",".join(betas)
    return headers


def _is_beta_header(name: object) -> bool:
    return isinstance(name, str) and name.lower() == BETA_HEADER


def _betas_in(value: object) -> list[str]:
    """The betas a string, or each item of a list, names: comma-separated, blanks dropped."""
    if value is None:
        return []
    items = value if isinstance(value, Iterable) and not isinstance(value, str | Mapping) else [value]
    return [beta for item in items for beta in (part.strip() for part in str(item).split(",")) if beta]


def _rename_options(fields: dict[str, Any]) -> None:
    for option, field in _RENAMED_OPTIONS:
        if option in fields:
            fields.setdefault(field, fields.pop(option))


def encode_tools(options: Mapping[str, Any]) -> dict[str, Any] | None:
    """Encode the call's tools and tool choice; None when there are neither.

    A function tool becomes a ``custom`` declaration, an ``mcp`` mapping an
    ``mcp_servers`` entry; any other tool is a provider declaration and is
    sent as given.
    """
    fields: dict[str, Any] = {}
    if tools := options.get("tools"):
        declarations: list[Any] = []
        mcp_servers: list[dict[str, Any]] = []
        for tool in normalize_tools(tools):
            if isinstance(tool, FunctionTool):
                declarations.append(
                    {
                        "type": "custom",
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.parameters(),
                    }
                )
            elif isinstance(tool, Mapping) and tool.get("type") == "mcp":
                mcp_servers.append(_mcp_server(tool))
            else:
                declarations.append(tool)
        if declarations:
            fields["tools"] = declarations
        if mcp_servers:
            fields["mcp_servers"] = mcp_servers
    if (tool_choice := _tool_choice(options)) is not None:
        fields["tool_choice"] = tool_choice
    return fields or None


def _mcp_server(tool: Mapping[str, Any]) -> dict[str, Any]:
    server: dict[str, Any] = {"type": "url", "name": tool.get("server_label", ""), "url": tool.get("server_url", "")}
    allowed_tools = tool.get("allowed_tools")
    if isinstance(allowed_tools, Sequence) and not isinstance(allowed_tools, str):
        server["tool_configuration"] = {"allowed_tools": [str(name) for name in allowed_tools]}
    headers = tool.get("headers")
    authorization = headers.get("authorization") if isinstance(headers, Mapping) else None
    if isinstance(authorization, str):
        server["authorization_token"] = authorization
    return server


def _tool_choice(options: Mapping[str, Any]) -> dict[str, Any] | None:
    if options.get("tool_choice") is None:
        return None
    tool_mode = validate_tool_mode(options.get("tool_choice"))
    if tool_mode is None:
        return None
    if "allowed_tools" in tool_mode:
        logger.warning("allowed_tools is not supported by Anthropic; the setting will be ignored")
    choice: dict[str, Any]
    match tool_mode.get("mode"):
        case "none":
            return {"type": "none"}
        case "auto":
            choice = {"type": "auto"}
        case "required" if "required_function_name" in tool_mode:
            choice = {"type": "tool", "name": tool_mode["required_function_name"]}
        case "required":
            choice = {"type": "any"}
        case _:
            logger.debug("Ignoring unsupported tool choice mode: %s", tool_mode)
            return None
    # The choice carries the parallel-call setting; it is no request field of its own.
    if (allow_multiple := options.get("allow_multiple_tool_calls")) is not None:
        choice["disable_parallel_tool_use"] = not allow_multiple
    return choice


def _output_config_with_format(output_config: Any, response_format: Any) -> dict[str, Any]:
    if output_config is None:
        merged: dict[str, Any] = {}
    elif isinstance(output_config, Mapping):
        merged = dict(output_config)
    else:
        raise ChatClientInvalidRequestException("output_config must be a mapping.")
    if merged.get("format") is not None:
        raise ChatClientInvalidRequestException(
            "response_format cannot be combined with explicit output_config.format."
        )
    merged["format"] = encode_output_format(response_format)
    return merged


def encode_output_format(response_format: type[BaseModel] | dict[str, Any]) -> dict[str, Any]:
    """The ``output_config.format`` value for a Pydantic model or a JSON-schema mapping.

    A mapping may carry its schema under ``json_schema.schema`` or ``schema``,
    or be the schema itself. The schema is sent closed to extra properties.
    """
    schema = _json_schema(response_format)
    if isinstance(schema, dict):
        schema = {**schema, "additionalProperties": False}
    return {"type": "json_schema", "schema": schema}


def _json_schema(response_format: type[BaseModel] | dict[str, Any]) -> Any:
    if not isinstance(response_format, dict):
        return response_format.model_json_schema()
    if "json_schema" in response_format:
        return response_format["json_schema"].get("schema", {})
    return response_format.get("schema", response_format)
