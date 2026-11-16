# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Chat history encoded as Chat Completions messages.

One kernel message can become several wire messages: a tool result is a
``tool`` record of its own, and the images tool results return follow them
in one user message. History written by another provider's hosted tools is
first reduced to text this wire can carry.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from chrys.foundation.models.history_markers import ANSWER_SEGMENT_KEY
from chrys.foundation.text.model_json import model_json
from chrys.kernel import TOOL_CALL_CONTENT_TYPES, Content, Message, is_image_content
from chrys.service.agent_middleware.events.hosted_tools import cross_provider_hosted_degradations
from chrys.service.llm.images import UNSUPPORTED_IMAGE_TEXT, WireImage, wire_image
from chrys.service.text_blocks import join_text_blocks, reconstruct_text_blocks, text_block_id

from .reasoning import (
    REASONING_FIELDS,
    contribution,
    fold,
    pad_reasoning_content,
    replayable_fields,
    replays_reasoning,
    stored_reasoning,
)

if TYPE_CHECKING:
    from chrys.foundation.reasoning_origin import ReasoningOrigin

    from .client import ChatCompletionsVariant

logger = logging.getLogger(__name__)

# Request-local keys: the images a tool result returned, waiting for the user
# message after the results, and the marker on a hosted-history summary.
_PENDING_IMAGES = "_chrys_pending_image_parts"
_HOSTED_SUMMARY = "_chrys_hosted_context_summary"
# Markers a text content carries for Chrys alone: the request-local summary
# marker and the segment the answer was shown in.
_PRIVATE_TEXT_KEYS = frozenset({_HOSTED_SUMMARY, ANSWER_SEGMENT_KEY})

# OpenAI checks ``name`` against ``^[^\s<|\\/>]+$`` with at most 64 characters.
# CJK and hyphens pass, so only the characters the rule forbids are removed.
_FORBIDDEN_NAME_CHARS = re.compile(r"[\s<|\\/>]+")

_AUDIO_FORMATS = ("wav", "mp3")

# What the model reads in place of media this wire has no part for.
_UNSUPPORTED_MEDIA_TEXT = "[{media_type} content omitted: the Chat Completions API does not accept it.]"


def sanitize_author_name(name: str | None) -> str | None:
    """The author name as a valid ``name`` field; ``None`` when nothing valid is left.

    Callers then omit the field instead of sending ``""``.
    """
    if not name:
        return None
    return _FORBIDDEN_NAME_CHARS.sub("", name)[:64] or None


def encode_messages(
    messages: Sequence[Message],
    *,
    variant: ChatCompletionsVariant,
    request_has_tools: bool = False,
    origin: ReasoningOrigin | None = None,
) -> list[dict[str, Any]]:
    """The request's ``messages``, sent to the endpoint *origin*.

    Each kernel message is encoded and canonicalized on its own, so the
    fragments of different messages never merge.
    """
    messages = degrade_hosted_history(messages)
    replay = replays_reasoning(messages, variant=variant, request_has_tools=request_has_tools)
    wire = [
        part
        for message in messages
        for part in canonicalize_tool_call_messages(
            encode_message(message, variant=variant, replay_reasoning=replay, origin=origin)
        )
    ]
    if replay and variant.reasoning_with_tools:
        pad_reasoning_content(wire)
    return insert_image_messages(wire)


def encode_message(
    message: Message,
    *,
    variant: ChatCompletionsVariant,
    replay_reasoning: bool = True,
    origin: ReasoningOrigin | None = None,
) -> list[dict[str, Any]]:
    """The wire messages for one kernel message, before canonicalization, sent to the endpoint *origin*."""
    if message.role in ("system", "developer"):
        # A plain string: some compatible endpoints reject a list for these
        # roles. Reasoning never replays on them.
        texts = [content.text for content in message.contents if content.type == "text" and content.text]
        if not texts:
            return []
        instruction: dict[str, Any] = {"role": message.role, "content": "\n".join(texts)}
        if name := sanitize_author_name(message.author_name):
            instruction["name"] = name
        return [instruction]
    if replay_reasoning and message.role == "assistant" and replayable_fields(message, origin=origin):
        return encode_reasoning_message(message, origin=origin)
    if variant.strict_messages:
        return _strict_messages(message)
    return _standard_messages(message, replay_reasoning=replay_reasoning, origin=origin)


def _standard_messages(
    message: Message, *, replay_reasoning: bool, origin: ReasoningOrigin | None
) -> list[dict[str, Any]]:
    """One wire message per content; calls in a row share one message.

    Replayed reasoning rides the next message with content or calls, and the
    copy kept in the message properties rides every one.
    """
    stored = stored_reasoning(message, origin=origin) if replay_reasoning else {}
    wire: list[dict[str, Any]] = []
    pending: dict[str, Any] = {}
    for content in message.contents:
        if content.type == "text_reasoning":
            if replay_reasoning and (found := contribution(content, origin=origin)) is not None:
                fold(pending, *found)
            continue
        if content.type == "function_call" and wire and "tool_calls" in wire[-1]:
            wire[-1]["tool_calls"].append(encode_content(content))
            continue
        fragment: dict[str, Any] = {"role": message.role}
        if message.role != "tool" and (name := sanitize_author_name(message.author_name)):
            fragment["name"] = name
        fragment.update(stored)
        if content.type == "function_result":
            fragment.update(_result_fields(content))
            wire.append(fragment)
            continue
        if content.type == "function_call":
            fragment["tool_calls"] = [encode_content(content)]
        else:
            fragment["content"] = [encode_content(content)]
        fragment.update(pending)
        pending = {}
        wire.append(fragment)
    if pending:
        if wire:
            wire[-1].update(pending)
        elif message.role == "assistant":
            wire.append(_reasoning_carrier(message, pending))
    _flatten_text_parts(wire)
    return wire


def _strict_messages(message: Message) -> list[dict[str, Any]]:
    """DeepSeek's layout: a message's fragments merge as far as their roles allow.

    Calls join the assistant message before them and other parts the
    previous message of the same role, and an assistant message with calls
    always has a ``content``. No reasoning rides here: a message whose
    reasoning replays is encoded by :func:`encode_reasoning_message`.
    """
    wire: list[dict[str, Any]] = []
    for content in message.contents:
        if content.type == "text_reasoning":
            continue
        last = wire[-1] if wire else None
        open_last = last is not None and "tool_call_id" not in last and last.get("role") == message.role
        fragment: dict[str, Any] = {"role": message.role}
        if message.role != "tool" and (name := sanitize_author_name(message.author_name)):
            fragment["name"] = name
        if content.type == "function_result":
            fragment.update(_result_fields(content))
        elif content.type == "function_call":
            call = encode_content(content)
            if last is not None and "tool_calls" in last:
                last["tool_calls"].append(call)
                continue
            if last is not None and open_last and message.role == "assistant":
                last["tool_calls"] = [call]
                continue
            fragment["tool_calls"] = [call]
            _default_call_content(fragment)
        elif last is not None and open_last and message.role != "tool":
            parts = last.setdefault("content", [])
            if not isinstance(parts, list):
                parts = [Content.from_text(text=str(parts)).to_dict(exclude_none=True)]
                last["content"] = parts
            cast("list[dict[str, Any]]", parts).append(encode_content(content))
            continue
        else:
            fragment["content"] = [encode_content(content)]
        wire.append(fragment)
    _flatten_text_parts(wire)
    return wire


def encode_reasoning_message(message: Message, *, origin: ReasoningOrigin | None = None) -> list[dict[str, Any]]:
    """An assistant message whose reasoning replays to the endpoint *origin*, as runs around its tool results.

    The text and calls between two results form one run and go out as one
    wire message, so text and its calls never reach the wire split. Its
    content stays a string: assistant content lists admit only text and
    refusal parts, and GLM documents a string. Other parts go out on their
    own ahead of their run's message; results stay ``tool`` records in
    source order, each right after the message with its call. Within a run,
    calls start a new text segment for text-block reconstruction and a
    hosted-history summary takes a segment of its own; reasoning splits
    none.

    A run's reasoning rides the run's message, next to the calls it led to,
    and never moves back across a result. A run of reasoning alone waits past
    the results that follow it (a message among them would separate calls
    from their results) and joins the next run, or ends the message as an
    empty carrier. Reasoning kept only in the message properties is placed
    once, for the whole message.
    """
    wire: list[dict[str, Any]] = []
    segments: list[list[tuple[str, str | None]]] = []
    calls: list[dict[str, Any]] = []
    reasoning: dict[str, Any] = {}
    supplied: set[str] = set()

    def end_segment() -> None:
        if segments and segments[-1]:
            segments.append([])

    def end_run(*, keep_reasoning_alone: bool = False) -> None:
        if not (segments or calls or (keep_reasoning_alone and reasoning)):
            return
        run = _role_and_name(message)
        run["content"] = join_text_blocks(block for segment in segments for block in reconstruct_text_blocks(segment))
        run.update(reasoning)
        if calls:
            run["tool_calls"] = list(calls)
        wire.append(run)
        segments.clear()
        calls.clear()
        reasoning.clear()

    for content in message.contents:
        kind = content.type
        if kind == "text_reasoning":
            if (found := contribution(content, origin=origin)) is not None:
                fold(reasoning, *found)
                supplied.add(found[0])
        elif kind == "function_call":
            calls.append(encode_content(content))
            end_segment()
        elif kind == "text":
            if content.text is None:
                continue
            summary = content.additional_properties.get(_HOSTED_SUMMARY) is True
            if summary:
                end_segment()
            if not segments:
                segments.append([])
            segments[-1].append((content.text, text_block_id(content.additional_properties)))
            if summary:
                end_segment()
        elif kind == "function_result":
            # Reasoning alone stays pending past the results.
            end_run()
            wire.append({"role": "tool", **_result_fields(content)})
        else:
            if kind in TOOL_CALL_CONTENT_TYPES:
                end_segment()
            standalone = _role_and_name(message)
            standalone["content"] = [encode_content(content)]
            wire.append(standalone)
    end_run(keep_reasoning_alone=True)
    _attach_reasoning(wire, message, stored_reasoning(message, skip=supplied, origin=origin))
    return wire


def _attach_reasoning(wire: list[dict[str, Any]], message: Message, fields: dict[str, Any]) -> None:
    """Put reasoning on one wire message of an assistant message.

    It rides the last message with ``tool_calls`` (DeepSeek and GLM document
    reasoning on the calls message), else the last text message of the same
    role that is not a result (never a multimodal list), else a new empty
    message.
    """
    if not fields:
        return
    carrier = next((candidate for candidate in reversed(wire) if "tool_calls" in candidate), None)
    if carrier is None:
        carrier = next((candidate for candidate in reversed(wire) if _text_message(candidate, message.role)), None)
    if carrier is None:
        wire.append(_reasoning_carrier(message, fields))
    else:
        carrier.update(fields)


def _text_message(candidate: dict[str, Any], role: str) -> bool:
    """Whether *candidate* is a non-result message of *role* whose content is text."""
    if candidate.get("role") != role or "tool_call_id" in candidate:
        return False
    content = candidate.get("content")
    return isinstance(content, str) or _text_parts_only(content)


def _reasoning_carrier(message: Message, fields: Mapping[str, Any]) -> dict[str, Any]:
    """An empty assistant message for reasoning nothing else can carry."""
    carrier: dict[str, Any] = {"role": message.role, "content": "", **fields}
    if name := sanitize_author_name(message.author_name):
        carrier["name"] = name
    return carrier


def _role_and_name(message: Message) -> dict[str, Any]:
    head: dict[str, Any] = {"role": message.role}
    if name := sanitize_author_name(message.author_name):
        head["name"] = name
    return head


def _result_fields(content: Content) -> dict[str, Any]:
    text, images = lower_function_result(content)
    fields: dict[str, Any] = {"tool_call_id": content.call_id, "content": text}
    if images:
        fields[_PENDING_IMAGES] = images
    return fields


def _text_parts_only(value: Any) -> bool:
    """Whether *value* is a content list holding text parts alone (an empty list too)."""
    return isinstance(value, list) and all(
        isinstance(part, Mapping) and cast("Mapping[str, Any]", part).get("type") == "text"
        for part in cast("list[object]", value)
    )


def _flatten_text_parts(wire: list[dict[str, Any]]) -> None:
    """Join content lists of text parts alone into one string.

    Some endpoints (Foundry Local) require a string for text-only messages;
    a list with any other part stays multimodal.
    """
    for message in wire:
        parts = message.get("content")
        if _text_parts_only(parts):
            message["content"] = "\n".join(
                text if isinstance(text := part.get("text", ""), str) else ""
                for part in cast("list[Mapping[str, Any]]", parts)
            )


# --- contents ---------------------------------------------------------------


def encode_content(content: Content) -> dict[str, Any]:
    """One content as a wire content part, tool call or result."""
    match content.type:
        case "function_call":
            arguments = model_json(content.arguments) if isinstance(content.arguments, Mapping) else content.arguments
            return {
                "id": content.call_id,
                "type": "function",
                "function": {"name": content.name, "arguments": arguments},
            }
        case "function_result":
            return {"tool_call_id": content.call_id, "content": "" if content.result is None else content.result}
        case "text":
            return _text_part(content)
        case "data" | "uri":
            return _media_part(content)
        case _:
            return content.to_dict(exclude_none=True)


def image_part(content: Content) -> dict[str, Any]:
    """An ``image_url`` part, or a text part saying the image was left out when the API can't read it."""
    if (image := wire_image(content)) is None:
        return {"type": "text", "text": UNSUPPORTED_IMAGE_TEXT}
    return _image_url_part(image, content)


def _image_url_part(image: WireImage, content: Content) -> dict[str, Any]:
    """An ``image_url`` part, with the content's ``detail`` when it sets one."""
    image_url: dict[str, Any] = {"url": image.uri}
    if isinstance(detail := content.additional_properties.get("detail"), str):
        image_url["detail"] = detail
    return {"type": "image_url", "image_url": image_url}


def _media_part(content: Content) -> dict[str, Any]:
    if content.has_top_level_media_type("image"):
        return image_part(content)
    if content.has_top_level_media_type("audio"):
        media_type = content.media_type or ""
        audio_format = next((name for name in _AUDIO_FORMATS if name in media_type), None)
        if audio_format is not None and content.uri is not None and content.uri.startswith("data:"):
            # Only the base64 payload; ``input_audio`` takes no link.
            data = content.uri.split(",", 1)[-1]
            return {"type": "input_audio", "input_audio": {"data": data, "format": audio_format}}
    elif (
        content.has_top_level_media_type("application") and content.uri is not None and content.uri.startswith("data:")
    ):
        # Every application type goes out as a file.
        file: dict[str, Any] = {"file_data": content.uri}
        if filename := content.additional_properties.get("filename"):
            file["filename"] = filename
        return {"type": "file", "file": file}
    media_type = content.media_type or "Media"
    # Debug only: history is encoded again for every request.
    logger.debug("Chat Completions has no content part for %s; a placeholder text goes out instead.", media_type)
    return {"type": "text", "text": _UNSUPPORTED_MEDIA_TEXT.format(media_type=media_type)}


def _text_part(content: Content) -> dict[str, Any]:
    """A text part without Chrys's private markers; history keeps them.

    Text parts of a multimodal message carry their other properties to the wire.
    """
    part = content.to_dict(exclude_none=True)
    properties = {
        key: value for key, value in part.get("additional_properties", {}).items() if key not in _PRIVATE_TEXT_KEYS
    }
    if properties:
        part["additional_properties"] = properties
    else:
        part.pop("additional_properties", None)
    return part


def lower_function_result(content: Content) -> tuple[str, list[dict[str, Any]]]:
    """A tool result's text, and the image parts the user message after the results carries.

    A ``tool`` record takes text only. Each image follows a label naming its
    call and item; other rich items are dropped with a warning.
    """
    if not content.items:
        return ("" if content.result is None else content.result), []
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    dropped = False
    for position, item in enumerate(content.items, start=1):
        if item.type == "text":
            texts.append(item.text or "")
        elif item.type in ("data", "uri"):
            if is_image_content(item) and isinstance(item.uri, str):
                if (image := wire_image(item)) is None:
                    texts.append(UNSUPPORTED_IMAGE_TEXT)
                    continue
                images.append({"type": "text", "text": f"Image from tool call {content.call_id}, item {position}:"})
                images.append(_image_url_part(image, item))
            else:
                dropped = True
    if dropped:
        logger.warning(
            "Chat Completions tool results carry only text and images; other rich items are left out "
            "(the Responses API client sends them)."
        )
    if images:
        texts.append("(see following user message for image)")
    return "\n".join(texts), images


def insert_image_messages(wire: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Move the images of tool results into one user message after each block of results."""
    arranged: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    for message in wire:
        parts = message.pop(_PENDING_IMAGES, None)
        if message.get("role") == "tool":
            if isinstance(parts, list):
                images.extend(cast("list[dict[str, Any]]", parts))
        elif images:
            arranged.append({"role": "user", "content": images})
            images = []
        arranged.append(message)
    if images:
        arranged.append({"role": "user", "content": images})
    return arranged


def degrade_hosted_history(messages: Sequence[Message]) -> Sequence[Message]:
    """History with every hosted tool content reduced to text.

    This wire has no hosted item types: a hosted content would arrive as an
    unknown part and an informational hosted call as a ``tool_calls`` entry
    no result answers, and strict endpoints reject both. No Chat Completions
    dialect hosts tools, so every hosted content is foreign here. A summary
    becomes text marked request-local; encoding removes the marker.
    """
    summaries = cross_provider_hosted_degradations(messages, target_provider="")
    if not summaries:
        return messages
    return [
        _degraded(message, summaries) if any(id(content) in summaries for content in message.contents) else message
        for message in messages
    ]


def _degraded(message: Message, summaries: Mapping[int, str | None]) -> Message:
    contents: list[Content] = []
    for content in message.contents:
        if id(content) not in summaries:
            contents.append(content)
        elif summary := summaries[id(content)]:
            contents.append(Content.from_text(text=summary, additional_properties={_HOSTED_SUMMARY: True}))
    return Message(
        message.role,
        contents,
        author_name=message.author_name,
        message_id=message.message_id,
        additional_properties=message.additional_properties,
        raw_representation=message.raw_representation,
    )


# --- canonical tool-call layout ----------------------------------------------


def canonicalize_tool_call_messages(wire: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair the wire messages of ONE kernel message around its tool calls.

    The encoder can split an assistant message with text and calls into a
    text message and a calls message; strict servers (vLLM) reject the pair,
    and the rejected history would stay in the session. Adjacent assistant
    fragments merge, one that cannot merge moves ahead of the calls message
    so results still follow their calls, and call arguments that are not a
    JSON object become ``"{}"``. Running per kernel message keeps turns from
    merging.
    """
    canonical: list[dict[str, Any]] = []
    for original in wire:
        message = dict(original)
        _repair_call_arguments(message)
        _default_call_content(message)
        previous = canonical[-1] if canonical else None
        if previous is not None and _mergeable(previous, message):
            _merge_into(previous, message)
            _default_call_content(previous)
        elif previous is not None and _goes_before_calls(previous, message):
            canonical.insert(len(canonical) - 1, message)
        else:
            canonical.append(message)
    return canonical


def _repair_call_arguments(message: dict[str, Any]) -> None:
    """Replace call arguments that are not a JSON object string with ``"{}"``.

    Some endpoints validate historical arguments: a call the model cut off at
    ``"{"`` got an error result, but resending it would fail the request.
    """
    calls = message.get("tool_calls")
    if message.get("role") != "assistant" or not isinstance(calls, list):
        return
    originals = cast("list[Any]", calls)
    repaired = [_repaired_call(call) for call in originals]
    if any(new is not old for new, old in zip(repaired, originals, strict=True)):
        message["tool_calls"] = repaired


def _repaired_call(call: Any) -> Any:
    if not isinstance(call, dict):
        return call
    function = cast("dict[str, Any]", call).get("function")
    if not isinstance(function, dict):
        return call
    arguments = cast("dict[str, Any]", function).get("arguments")
    valid = _json_object_arguments(arguments)
    if valid == arguments:
        return call
    return {**cast("dict[str, Any]", call), "function": {**cast("dict[str, Any]", function), "arguments": valid}}


def _json_object_arguments(arguments: Any) -> str:
    """The arguments as a JSON object string; ``"{}"`` for anything else."""
    if isinstance(arguments, Mapping):
        try:
            return model_json(arguments)
        except TypeError, ValueError:
            return "{}"
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        # A bare ValueError is an integer literal past Python's integer-string
        # conversion limit: unrepresentable, like malformed JSON.
        except ValueError:
            return "{}"
        if isinstance(decoded, dict):
            return arguments
    return "{}"


def _default_call_content(message: dict[str, Any]) -> None:
    """Give an assistant message with calls an explicit empty ``content``."""
    if message.get("role") == "assistant" and "tool_calls" in message and "content" not in message:
        message["content"] = ""


def _is_empty(value: Any) -> bool:
    return value in (None, "", [])


def _mergeable(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Whether two adjacent fragments are one assistant message the encoder split."""
    if previous.get("role") != "assistant" or current.get("role") != "assistant":
        return False
    if "tool_call_id" in previous or "tool_call_id" in current:
        return False
    # A message with reasoning was assembled whole, its reasoning next to its
    # calls; merging would duplicate or move it, or take in parts its shape
    # rejects.
    if any(name in previous or name in current for name in REASONING_FIELDS):
        return False
    # Non-empty string and list contents cannot combine without losing one.
    # Empty content combines with either, so a calls-only fragment still
    # merges with a multimodal one.
    first, second = previous.get("content"), current.get("content")
    if not _is_empty(first) and not _is_empty(second) and isinstance(first, str) != isinstance(second, str):
        return False
    return "tool_calls" in previous or "tool_calls" in current


def _goes_before_calls(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Whether a fragment that could not merge belongs ahead of the calls message before it.

    Results must directly follow the message with their calls, so a
    content-only assistant fragment left over (a string and list clash)
    moves in front of it, the layout the reasoning encoder also produces.
    """
    return (
        previous.get("role") == "assistant"
        and "tool_calls" in previous
        and "tool_call_id" not in previous
        and current.get("role") == "assistant"
        and "tool_calls" not in current
        and "tool_call_id" not in current
    )


def _merge_into(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Fold a later assistant fragment into the one before it."""
    if calls := source.get("tool_calls"):
        target.setdefault("tool_calls", []).extend(calls)
    added = source.get("content")
    if not _is_empty(added):
        present = target.get("content")
        if _is_empty(present):
            target["content"] = added
        elif isinstance(present, str) and isinstance(added, str):
            target["content"] = present + added
        elif isinstance(present, list) and isinstance(added, list):
            target["content"] = [*cast("list[Any]", present), *cast("list[Any]", added)]
    for key, value in source.items():
        if key not in ("role", "content", "tool_calls") and _is_empty(target.get(key)):
            target[key] = value
