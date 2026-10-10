# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Whole completions and their usage read back as chat responses.

The stream reuses the pieces that describe one choice: its text, its
metadata and the usage.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, cast

from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_message_custom_tool_call import ChatCompletionMessageCustomToolCall

from chrys.foundation.errors import ProviderResponseError
from chrys.kernel import CONTEXT_WINDOW_FILLED_KEY, ChatResponse, Content, FinishReason, Message, UsageDetails
from chrys.service.llm.openai_timestamps import openai_created_at_iso

from .reasoning import message_reasoning, message_reasoning_props
from .validation import raise_invalid_response

if TYPE_CHECKING:
    from openai.types import CompletionUsage
    from openai.types.chat.chat_completion_chunk import ChatCompletionChunk, ChoiceDelta
    from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
    from openai.types.chat.chat_completion_message import ChatCompletionMessage

    from chrys.foundation.reasoning_origin import ReasoningOrigin

    from .client import ChatCompletionsVariant

logger = logging.getLogger(__name__)

_PAYLOAD_PREVIEW_LIMIT = 2000
_TRUNCATED = "...[truncated]"

# Finish reasons compatible services spell their own way (GLM ``sensitive``).
_FINISH_REASON_SPELLINGS: Final[Mapping[str, str]] = {"end": "stop", "sensitive": "content_filter"}
# Finish reasons that report the service failed the completion; another
# request may well succeed.
_FAILED_FINISH_REASONS: Final = frozenset({"network_error", "insufficient_system_resource"})
# The model filled its context window as it generated. With part of an
# answer out, the reply was cut off as at its output limit; with none, no
# attempt of the same request can fit an answer.
_CONTEXT_WINDOW_FILLED: Final = "model_context_window_exceeded"

# The usage breakdowns, in reporting order: the details object, the prefix of
# the keys its counts are reported under, and each count with the kernel key
# it also fills and whether a zero is kept. The SDK fields are read directly,
# so a details value of the wrong shape fails the decode. Billed explicit
# prompt caching reports ``cache_write_tokens``; services that omit it leave
# the SDK's optional field unset.
_BREAKDOWNS: tuple[tuple[str, str, tuple[tuple[str, str | None, bool], ...]], ...] = (
    (
        "completion_tokens_details",
        "completion",
        (
            ("accepted_prediction_tokens", None, False),
            ("audio_tokens", None, False),
            ("reasoning_tokens", "reasoning_output_token_count", True),
            ("rejected_prediction_tokens", None, False),
        ),
    ),
    (
        "prompt_tokens_details",
        "prompt",
        (
            ("audio_tokens", None, False),
            ("cached_tokens", "cache_read_input_token_count", True),
            ("cache_write_tokens", "cache_creation_input_token_count", True),
        ),
    ),
)


def ensure_choices(response: Any) -> None:
    """Reject a decoded completion whose ``choices`` is not a list.

    The raw-response check rejects such a body already; this one guards the
    decoder itself. A gateway may answer with an error envelope such as
    ``{"error": "rate limit"}``, which the SDK turns into a completion with
    ``choices=None`` that would fail later as ``'NoneType' object is not
    iterable``. The SDK model keeps unknown fields, so the payload quoted in
    the error still shows what the gateway said.
    """
    choices = response.choices
    if isinstance(choices, list):
        return
    try:
        payload = response.model_dump_json()
    except Exception:
        payload = repr(response)
    if len(payload) > _PAYLOAD_PREVIEW_LIMIT:
        payload = payload[: _PAYLOAD_PREVIEW_LIMIT - len(_TRUNCATED)] + _TRUNCATED
    problem = (
        "is missing the required 'choices' array"
        if choices is None
        else f"'choices' is {type(choices).__name__}; expected an array"
    )
    raise_invalid_response(f"OpenAI Chat Completions response {problem}. Parsed payload: {payload}")


def finish_reason(value: object, *, answered: bool) -> str | None:
    """The finish reason a choice reports, in the spelling the kernel reads; an empty string reports none.

    A context window the model filled once its choice *answered* (text, a
    refusal or a call) reads as ``length``, as the output it cut off is kept;
    the response then also carries ``CONTEXT_WINDOW_FILLED_KEY``
    (:func:`filled_window`).
    """
    if not isinstance(value, str) or not value:
        return None
    if value == _CONTEXT_WINDOW_FILLED and answered:
        return "length"
    return _FINISH_REASON_SPELLINGS.get(value, value)


def filled_window(value: object, read_as: str | None) -> bool:
    """Whether a choice that reported *value* filled its context window and was read as cut off."""
    return value == _CONTEXT_WINDOW_FILLED and read_as == "length"


def has_answer(message: ChatCompletionMessage | ChoiceDelta | None) -> bool:
    """Whether a message or delta carries answer text, a refusal or a call to a named function.

    Text that is whitespace alone is no answer (reasoning models often send
    some as they switch to answering), nor is a call that names no function
    or a custom tool call, which is skipped.
    """
    if message is None:
        return False
    content = message.content
    return (
        (isinstance(content, str) and bool(content.strip()))
        or has_refusal(message)
        or any(_names_a_function(call) for call in message.tool_calls or ())
    )


def _names_a_function(call: object) -> bool:
    name = getattr(getattr(call, "function", None), "name", None)
    return isinstance(name, str) and bool(name)


def finish_failure(
    reasons: Iterable[str | None], *, usage_details: UsageDetails | None = None
) -> ProviderResponseError | None:
    """The provider's error for the first finish reason that reports a failure, or None.

    A refusal with tool calls outranks it: callers check for that first.
    """
    for reason in reasons:
        if reason in _FAILED_FINISH_REASONS:
            return ProviderResponseError(
                reason,
                f"The service ended the completion with finish reason {reason!r}.",
                retryable=True,
                usage_details=usage_details,
            )
        if reason == _CONTEXT_WINDOW_FILLED:
            return ProviderResponseError(
                reason,
                "The model filled its context window before it produced an answer.",
                retryable=False,
                invalidates_continuation_token=True,
                usage_details=usage_details,
            )
    return None


def refused_calls_error(
    observed_contents: Sequence[Content] = (), *, usage_details: UsageDetails | None = None
) -> ProviderResponseError:
    """The failure of a response that refused or was filtered, yet asks for tool calls.

    The calls are never run: the response's own verdict is that it should
    not go on, and that verdict stands even when the response also failed
    or was cut off. *observed_contents* is the hosted work the response
    showed but never yielded.
    """
    return ProviderResponseError(
        "content_filter",
        "The response was refused or filtered, so the tool calls it requested were not run.",
        retryable=False,
        invalidates_continuation_token=True,
        observed_contents=tuple(observed_contents),
        usage_details=usage_details,
    )


def has_refusal(message: ChatCompletionMessage | ChoiceDelta | None) -> bool:
    """Whether a message or delta carries an explicit refusal."""
    refusal = getattr(message, "refusal", None)
    return isinstance(refusal, str) and bool(refusal)


def decode_completion(
    response: ChatCompletion,
    options: Mapping[str, Any],
    *,
    variant: ChatCompletionsVariant,
    origin: ReasoningOrigin | None = None,
) -> ChatResponse:
    """A whole completion as a chat response with one assistant message per choice.

    Tool calls of a response that refused or was filtered fail the whole
    response before it lands. Reasoning the endpoint *origin* alone can read
    is stamped with it.
    """
    ensure_choices(response)
    metadata = response_metadata(response)
    messages: list[Message] = []
    finish: FinishReason | None = None
    usage = decode_usage(response.usage, variant=variant) if response.usage else None
    reasons = [finish_reason(choice.finish_reason, answered=has_answer(choice.message)) for choice in response.choices]
    calls = [
        _function_calls(choice.message, cut_off=reason == "length")
        for choice, reason in zip(response.choices, reasons, strict=True)
    ]
    refused = "content_filter" in reasons or any(has_refusal(choice.message) for choice in response.choices)
    if refused and any(calls):
        raise refused_calls_error(usage_details=usage)
    if (failure := finish_failure(reasons, usage_details=usage)) is not None:
        raise failure
    for choice, reason, choice_calls in zip(response.choices, reasons, calls, strict=True):
        metadata.update(choice_metadata(choice))
        if filled_window(choice.finish_reason, reason):
            metadata[CONTEXT_WINDOW_FILLED_KEY] = True
        if reason is not None:
            finish = FinishReason(reason)
        # Text, then calls, then reasoning: unlike a delta, a whole message
        # has no chunk boundary that could split its text.
        contents = [*text_contents(choice), *choice_calls, *message_reasoning(choice.message, origin=origin)]
        messages.append(
            Message(
                role="assistant",
                contents=contents,
                additional_properties=message_reasoning_props(choice.message, origin=origin),
            )
        )
    return ChatResponse(
        messages=messages,
        response_id=response.id,
        created_at=openai_created_at_iso(response.created),
        model=response.model,
        finish_reason=finish,
        usage_details=usage,
        response_format=options.get("response_format"),
        additional_properties=metadata,
    )


def decode_usage(usage: CompletionUsage, *, variant: ChatCompletionsVariant) -> UsageDetails:
    """Token counts and the breakdowns the usage reports.

    Reasoning, cache-read and cache-write counts are kept at zero; other
    breakdowns only when non-zero.
    """
    details = UsageDetails(
        input_token_count=usage.prompt_tokens,
        output_token_count=usage.completion_tokens,
        total_token_count=usage.total_tokens,
    )
    counts = cast("dict[str, Any]", details)
    for group, prefix, entries in _BREAKDOWNS:
        if not (source := getattr(usage, group)):
            continue
        for name, kernel_key, keep_zero in entries:
            count = getattr(source, name)
            if count is None or not (count or keep_zero):
                continue
            counts[f"{prefix}/{name}"] = count
            if kernel_key is not None:
                counts[kernel_key] = count
    if "cache_read_input_token_count" not in details:
        # Kimi reports cache reads as a top-level ``cached_tokens`` (zero kept).
        cached = (usage.model_extra or {}).get("cached_tokens")
        if isinstance(cached, int) and not isinstance(cached, bool):
            counts["prompt/cached_tokens"] = cached
            counts["cache_read_input_token_count"] = cached
    if variant.reports_prompt_cache_hits:
        add_deepseek_cache_usage(details, usage)
    return details


def add_deepseek_cache_usage(details: UsageDetails, usage: Any) -> None:
    """Add DeepSeek's prompt-cache hits, reported top-level as ``prompt_cache_hit_tokens``.

    The kernel keys do not cover it, so it goes under a namespaced key the
    usage middleware lets through. The Responses decoder reads it too.
    """
    hits = getattr(usage, "prompt_cache_hit_tokens", None)
    if hits is None:
        hits = (getattr(usage, "model_extra", None) or {}).get("prompt_cache_hit_tokens")
    if hits is not None:
        cast("dict[str, Any]", details)["deepseek.prompt_cache_hit_tokens"] = int(hits)


def text_contents(choice: Choice | ChunkChoice) -> list[Content]:
    """A choice's answer text and refusal, each when it is a non-empty string."""
    message = choice.message if isinstance(choice, Choice) else choice.delta
    contents: list[Content] = []
    if text := message.content:
        if isinstance(text, str):
            contents.append(Content.from_text(text=text, raw_representation=choice))
        else:
            logger.debug("Ignoring non-string Chat Completions content of type %s", type(text).__name__)
    if isinstance(refusal := message.refusal, str) and refusal:
        contents.append(Content.from_text(text=refusal, raw_representation=choice))
    return contents


def response_metadata(payload: ChatCompletion | ChatCompletionChunk) -> dict[str, Any]:
    """The metadata a completion or chunk adds to the response."""
    return {"system_fingerprint": getattr(payload, "system_fingerprint", None)}


def choice_metadata(choice: Choice | ChunkChoice) -> dict[str, Any]:
    """The metadata one choice adds to the response."""
    return {"logprobs": getattr(choice, "logprobs", None)}


def _function_calls(message: ChatCompletionMessage | None, *, cut_off: bool) -> list[Content]:
    """The message's function calls; custom tool calls are skipped.

    A call without a name is kept for response validation to retry the
    request, unless the choice was *cut_off* at the length limit: as in a
    stream, the model never finished it, and the finish reason says why.
    """
    if not message or not message.tool_calls:
        return []
    contents: list[Content] = []
    for call in message.tool_calls:
        if isinstance(call, ChatCompletionMessageCustomToolCall) or not call.function:
            continue
        if not call.function.name and cut_off:
            logger.warning("Discarding truncated tool call without a name")
            continue
        contents.append(
            Content.from_function_call(
                call_id=call.id or "",
                name=call.function.name or "",
                arguments=call.function.arguments or "",
                raw_representation=call.function,
            )
        )
    return contents
