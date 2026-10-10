# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Responses read back as chat responses.

:func:`decode_response` reads a blocking response. The stream (:mod:`.stream`)
reuses the pieces that describe whole items or the response itself: reasoning
items, client-executed tool calls, usage, the conversation handle, the
continuation token, the finish reason, and the failure a response that
stopped running reports.

A failed or cancelled response raises instead of landing, and so does one
that refused or was filtered yet asks for function calls: their calls never
run. Either error carries the hosted work the response showed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal, cast

from openai.types.responses.parsed_response import ParsedResponse

from chrys.foundation.errors import ProviderResponseError, in_band_failure_retryable
from chrys.foundation.hosted_tools import OPENAI_HOSTED_WIRE_ITEM_KEY
from chrys.kernel import (
    OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY,
    Annotation,
    ChatResponse,
    Content,
    ContinuationToken,
    Message,
    TextSpanRegion,
    UsageDetails,
)
from chrys.service.llm.chat_completions.decode import add_deepseek_cache_usage, refused_calls_error
from chrys.service.llm.openai_timestamps import openai_created_at_iso
from chrys.service.profiles.models.options import effective_store_option

from .hosted import decode_hosted_item, to_payload

if TYPE_CHECKING:
    from openai.types.responses.response import Response
    from openai.types.responses.response_usage import ResponseUsage
    from pydantic import BaseModel

    from chrys.foundation.reasoning_origin import ReasoningOrigin

    from .client import ResponsesVariant

logger = logging.getLogger(__name__)

# The output-message fields its text replays with, so a replayed message
# keeps the identity it was produced under.
ENVELOPE_FIELDS = ("id", "status", "phase")
# Statuses of a response that is still running, so a token can resume it.
RUNNING_STATUSES = ("in_progress", "queued")
# Output items the client reads itself: never hosted work.
_CLIENT_ITEM_TYPES = frozenset({"message", "reasoning", "function_call", "custom_tool_call", "apply_patch_call"})


class OpenAIContinuationToken(ContinuationToken):
    """Where to pick up a background response: the id to retrieve."""

    response_id: str


def decode_response(
    response: Response | ParsedResponse[BaseModel],
    options: Mapping[str, Any],
    *,
    variant: ResponsesVariant,
    origin: ReasoningOrigin | None = None,
) -> ChatResponse:
    """A blocking response as one assistant message plus response metadata.

    Its reasoning is stamped with *origin*, the endpoint that sent it.
    """
    # ParsedResponse's type argument is erased at runtime; requests only ever
    # ask the SDK to parse into Pydantic models.
    parsed = cast("BaseModel | None", response.output_parsed) if isinstance(response, ParsedResponse) else None
    # Log probabilities of the text parts collect here.
    metadata: dict[str, Any] = dict(response.metadata or {})
    try:
        output = response.output
    except AttributeError:
        output = []
    contents: list[Content] = []
    for item in output:
        contents.extend(_decode_item(item, metadata, variant.hosted_provider))
    if origin is not None:
        for content in contents:
            if content.type == "text_reasoning":
                origin.stamp(content.additional_properties)
    hosted = [content for content in contents if content.provider_hosted]
    usage = decode_usage(response.usage, variant=variant) if response.usage else None
    reason = finish_reason(response)
    if (reason == "content_filter" or any(map(refuses, output))) and any(map(is_function_call, output)):
        raise refused_calls_error(hosted, usage_details=usage)
    if (failure := response_failure(response, observed=hosted, usage_details=usage)) is not None:
        raise failure

    fields: dict[str, Any] = {
        "response_id": response.id,
        "created_at": timestamp(response.created_at),
        "messages": Message(role="assistant", contents=contents),
        "model": response.model,
        "additional_properties": metadata,
        "raw_representation": response,
    }
    store = effective_store_option(options)
    if conversation_id := conversation_handle(response, store=store, variant=variant):
        fields["conversation_id"] = conversation_id
    if usage:
        fields["usage_details"] = usage
    if parsed:
        fields["value"] = parsed
    elif response_format := options.get("response_format"):
        fields["response_format"] = response_format
    if response.status in RUNNING_STATUSES and (token := continuation_token(response.id, store=store, variant=variant)):
        fields["continuation_token"] = token
    if reason:
        fields["finish_reason"] = reason
    return ChatResponse(**fields)


def response_failure(
    response: Any, *, observed: Iterable[Content] = (), usage_details: UsageDetails | None = None
) -> ProviderResponseError | None:
    """The failure a response reports, or None when it did not fail.

    A failed or cancelled response, or one carrying an error, will not
    change any more, so its error drops the continuation token. The error
    code decides whether sending the request anew may succeed; a cancelled
    response is not sent again. *observed* is the hosted work it showed but
    never yielded; *usage_details* the usage it reported. A refusal with
    tool calls outranks the failure: callers check for that first.
    """
    status = getattr(response, "status", None)
    error = getattr(response, "error", None)
    code, message = _error_field(error, "code"), _error_field(error, "message")
    if code is None and message is None and status not in ("failed", "cancelled"):
        return None
    if status == "cancelled":
        code = code or "cancelled"
        retryable = False
    else:
        code = code or "server_error"
        retryable = in_band_failure_retryable(code)
    return ProviderResponseError(
        code,
        message or f"The service reported the response as {status or 'failed'}.",
        retryable=retryable,
        invalidates_continuation_token=True,
        observed_contents=tuple(observed),
        usage_details=usage_details,
    )


def _error_field(error: Any, name: str) -> str | None:
    value = error.get(name) if isinstance(error, Mapping) else getattr(error, name, None)
    return value if isinstance(value, str) and value else None


def is_function_call(item: Any) -> bool:
    """Whether an output item is a function call this client runs."""
    return getattr(item, "type", None) == "function_call"


def refuses(item: Any) -> bool:
    """Whether an output item is a message carrying a non-empty refusal."""
    if getattr(item, "type", None) != "message":
        return False
    return any(
        getattr(part, "type", None) == "refusal" and bool(getattr(part, "refusal", None))
        for part in getattr(item, "content", None) or []
    )


def hosted_contents(items: Iterable[Any], provider: str) -> list[Content]:
    """The hosted call and result contents of output items; other items add nothing."""
    contents: list[Content] = []
    for item in items:
        if getattr(item, "type", None) not in _CLIENT_ITEM_TYPES:
            contents.extend(decode_hosted_item(item, provider) or [])
    return contents


def _decode_item(item: Any, metadata: dict[str, Any], provider: str) -> list[Content]:
    match item.type:
        case "message":
            return _message_parts(item, metadata)
        case "reasoning":
            return decode_reasoning_item(item, streamed=False)
        case "function_call":
            return [
                Content.from_function_call(
                    call_id=item.call_id,
                    name=item.name,
                    arguments=item.arguments,
                    additional_properties={"fc_id": item.id, "status": item.status},
                    raw_representation=item,
                )
            ]
        case "custom_tool_call":
            return [decode_client_tool_call(item, name=item.name, arguments=item.input)]
        case "apply_patch_call":
            return [decode_client_tool_call(item, name="apply_patch", arguments=getattr(item, "operation", None))]
        case _:
            return decode_hosted_item(item, provider) or []


def _message_parts(item: Any, metadata: dict[str, Any]) -> list[Content]:
    envelope = output_message_envelope(item)
    properties = {OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY: envelope} if envelope else None
    parts: list[Content] = []
    for part in item.content:
        if part.type == "output_text":
            text = Content.from_text(text=part.text, raw_representation=part, additional_properties=properties)
            metadata.update(logprobs_metadata(part))
            if part.annotations:
                text.annotations = [
                    annotation for annotation in map(_citation, part.annotations) if annotation is not None
                ]
            parts.append(text)
        elif part.type == "refusal":
            parts.append(
                Content.from_text(text=part.refusal, raw_representation=part, additional_properties=properties)
            )
    return parts


def _citation(annotation: Any) -> Annotation | None:
    """A citation annotation of a blocking response's text part.

    Streamed parts describe the same citations in another shape
    (:func:`.stream.streamed_citation`); the two are kept as they are.
    """
    kind = annotation.type
    if kind == "url_citation":
        return Annotation(
            type="citation",
            title=annotation.title,
            url=annotation.url,
            annotated_regions=[_span(annotation)],
            raw_representation=annotation,
        )
    if kind == "container_file_citation":
        return Annotation(
            type="citation",
            file_id=annotation.file_id,
            url=annotation.filename,
            additional_properties={"container_id": annotation.container_id},
            annotated_regions=[_span(annotation)],
            raw_representation=annotation,
        )
    if kind == "file_citation":
        return Annotation(
            type="citation",
            url=annotation.filename,
            file_id=annotation.file_id,
            raw_representation=annotation,
            additional_properties={"index": annotation.index},
        )
    if kind == "file_path":
        return Annotation(
            type="citation",
            file_id=annotation.file_id,
            additional_properties={"index": annotation.index},
            raw_representation=annotation,
        )
    logger.debug("Unparsed annotation type: %s", kind)
    return None


def _span(annotation: Any) -> TextSpanRegion:
    return TextSpanRegion(type="text_span", start_index=annotation.start_index, end_index=annotation.end_index)


def output_message_envelope(item: Any) -> dict[str, str]:
    """The envelope fields an output message carries."""
    return {name: value for name in ENVELOPE_FIELDS if isinstance(value := getattr(item, name, None), str) and value}


def decode_reasoning_item(item: Any, *, streamed: bool) -> list[Content]:
    """A reasoning item's text parts, then its summaries.

    An item with neither still yields one empty content, so its encrypted
    payload and its place among the outputs survive. Two differences between
    the blocking and the streamed shape stay as they are: a blocking item
    puts its payload on the first content only and pairs each text part with
    the summary at its index; a streamed one puts the payload on every
    content and pairs nothing.
    """
    item_id = (getattr(item, "id", None) or None) if streamed else item.id
    payload = getattr(item, "encrypted_content", None)
    summaries = getattr(item, "summary", None) or []
    contents: list[Content] = []

    def add(text: str, raw: Any, properties: dict[str, Any] | None = None) -> None:
        contents.append(
            Content.from_text_reasoning(
                id=item_id,
                text=text,
                protected_data=payload if streamed or not contents else None,
                raw_representation=raw,
                additional_properties=properties,
            )
        )

    for index, part in enumerate(getattr(item, "content", None) or []):
        properties: dict[str, Any] = {"reasoning_text": True}
        if not streamed and index < len(summaries):
            properties["summary"] = summaries[index]
        add(part.text, part, properties)
    for summary in summaries:
        add(summary.text, summary)
    if not contents:
        add("", item)
    return contents


def decode_client_tool_call(item: Any, *, name: str, arguments: Any) -> Content:
    """A custom or apply-patch call, which the client would have to run.

    Nothing here runs it, so it is informational: history sends the wire
    item back and replay answers an apply-patch call with a failure.
    """
    item_type = str(getattr(item, "type", ""))
    properties: dict[str, Any] = {"item_type": item_type, OPENAI_HOSTED_WIRE_ITEM_KEY: to_payload(item)}
    if item_type == "custom_tool_call":
        call_id = getattr(item, "call_id", "") or ""
        if item_id := getattr(item, "id", None):
            properties["item_id"] = item_id
        if namespace := getattr(item, "namespace", None):
            properties["namespace"] = namespace
    else:
        item_id = getattr(item, "id", "") or ""
        call_id = getattr(item, "call_id", None) or item_id
        properties["item_id"] = item_id
        properties["status"] = getattr(item, "status", None)
        properties["execution"] = to_payload(getattr(item, "execution", None))
        if created_by := getattr(item, "created_by", None):
            properties["created_by"] = created_by
    return Content.from_function_call(
        call_id=call_id,
        name=name,
        arguments=to_payload(arguments),
        informational_only=True,
        additional_properties=properties,
        raw_representation=item,
    )


def decode_usage(usage: ResponseUsage, *, variant: ResponsesVariant) -> UsageDetails:
    """Token counts, with cache reads and writes and reasoning tokens when reported (zero included)."""
    details = UsageDetails(
        input_token_count=usage.input_tokens,
        output_token_count=usage.output_tokens,
        total_token_count=usage.total_tokens,
    )
    if inputs := usage.input_tokens_details:
        if (cached := getattr(inputs, "cached_tokens", None)) is not None:
            details["openai.cached_input_tokens"] = cached  # type: ignore[typeddict-unknown-key]
            details["cache_read_input_token_count"] = cached
        # Billed explicit prompt caching reports it. The SDK's model requires
        # it, but its response parsing leaves it unset when an
        # OpenAI-compatible service omits it.
        if (written := getattr(inputs, "cache_write_tokens", None)) is not None:
            details["openai.cache_write_tokens"] = written  # type: ignore[typeddict-unknown-key]
            details["cache_creation_input_token_count"] = written
    outputs = usage.output_tokens_details
    if outputs and (reasoning := getattr(outputs, "reasoning_tokens", None)) is not None:
        details["openai.reasoning_tokens"] = reasoning  # type: ignore[typeddict-unknown-key]
        details["reasoning_output_token_count"] = reasoning
    if variant.reports_prompt_cache_hits:
        add_deepseek_cache_usage(details, usage)
    return details


def logprobs_metadata(source: Any) -> dict[str, Any]:
    """The response metadata a text part or delta adds: its log probabilities."""
    logprobs = getattr(source, "logprobs", None)
    return {"logprobs": logprobs} if logprobs else {}


def conversation_handle(response: Any, *, store: Any, variant: ResponsesVariant) -> str | None:
    """The id the next request continues from: the conversation, else the response.

    None when the service keeps nothing, as when the request opted out of
    storing it.
    """
    if variant.stateless or store is False:
        return None
    if response.conversation and response.conversation.id:
        return response.conversation.id
    return response.id


def continuation_token(response_id: str, *, store: Any, variant: ResponsesVariant) -> OpenAIContinuationToken | None:
    """A token to resume an unfinished response, if it can be retrieved.

    An unstored response cannot: retrieving it fails, so a token would turn
    every reconnect into an error instead of a new request.
    """
    if variant.stateless or store is False:
        return None
    return OpenAIContinuationToken(response_id=response_id)


def finish_reason(response: Any) -> Literal["length", "content_filter"] | None:
    """``length`` for a response cut off at its output cap, ``content_filter`` for one a filter stopped.

    Truncation handling then treats a cutoff like the other protocols' one,
    not as an empty response to retry.
    """
    if response.status != "incomplete":
        return None
    match getattr(getattr(response, "incomplete_details", None), "reason", None):
        case "max_output_tokens":
            return "length"
        case "content_filter":
            return "content_filter"
        case _:
            return None


def timestamp(created_at: Any) -> str | None:
    """A response's creation time in the chat response's UTC format, ``None`` when it is no usable time."""
    return openai_created_at_iso(created_at)
