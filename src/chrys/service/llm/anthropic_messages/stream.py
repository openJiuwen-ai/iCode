# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Decode one response's stream events into chat updates.

A local ``tool_use`` block streams its input as JSON fragments; the call is
emitted once, complete. From the first such block on, every later block is
held too; hosted work held that way is reported at once as
:class:`~chrys.foundation.hosted_tools.HeldHostedEvidence`. Everything held is
released in block order when the stream says how the message ends: before the
message delta that carries its stop reason (some consumers treat its finish
reason as terminal), or at the message stop.

A stream that ends before either lost the rest of the message, and the calls
it held may be cut short: :meth:`StreamState.finish` raises a retryable
truncation and none of them runs. A message that stops for a refusal yet asks
for calls raises too: its own verdict is that it should not go on.

Server-tool and MCP calls are emitted when their block starts and again,
refreshed in place, as their input streams.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from anthropic.types.beta import (
    BetaRawContentBlockDeltaEvent,
    BetaRawContentBlockStartEvent,
    BetaRawMessageStreamEvent,
)

from chrys.foundation.errors import ProviderResponseError
from chrys.foundation.hosted_tools import HeldHostedEvidence, HostedToolPhase
from chrys.foundation.reasoning_origin import ReasoningOrigin
from chrys.kernel import ChatResponseUpdate, Content, UsageDetails, normalize_stream_usage
from chrys.service.llm.refusals import refused_calls_error

from .decode import (
    decode_blocks,
    decode_stop_reason,
    decode_usage,
    log_input_transformations,
    stream_context_input,
    token_count,
)
from .server_tools import apply_streamed_input

logger = logging.getLogger(__name__)


@dataclass
class _ToolUse:
    """A streamed ``tool_use`` block whose input is still arriving."""

    call_id: str
    name: str
    start_input: Any
    """The input on the block start, used when no fragment follows."""
    raw_parts: list[Any]
    input_parts: list[str] = field(default_factory=list)

    def add_input(self, partial_json: str, raw: Any) -> None:
        self.input_parts.append(partial_json)
        self.raw_parts.append(raw)

    def to_content(self) -> Content:
        arguments = "".join(self.input_parts) if self.input_parts else self.start_input
        raw = self.raw_parts[0] if len(self.raw_parts) == 1 else self.raw_parts
        return Content.from_function_call(
            call_id=self.call_id, name=self.name, arguments=arguments, raw_representation=raw
        )


class StreamState:
    """The decoding state of one streamed response."""

    def __init__(self, *, origin: ReasoningOrigin | None = None) -> None:
        # The endpoint that sends the stream, stamped on its thinking.
        self._origin = origin
        self._tool_uses: dict[int, _ToolUse] = {}
        self._held: dict[int, list[ChatResponseUpdate]] = {}
        self._hold_from: int | None = None
        """Index of the first unreleased ``tool_use`` block; later blocks are held."""
        self._reported: set[int] = set()
        """Ids of the held hosted contents already reported; held, they stay alive."""
        self._hosted_blocks: set[int] = set()
        self._hosted_calls: dict[int, Content] = {}
        self._hosted_input: dict[int, list[str]] = {}
        self._first_cache_read: int | None = None
        self._usage: UsageDetails | None = None
        """The usage reported so far, the latest value of each key."""
        self._stop_reason: str | None = None
        self._ended = False
        """The stream said how the message ends: a stop reason or the message stop."""

    def updates_for(self, event: BetaRawMessageStreamEvent) -> Iterator[ChatResponseUpdate]:
        """The updates *event* releases, in the order the consumer sees them."""
        update = self._decode(event)
        if (
            self._ended
            and event.type in ("message_delta", "message_stop")
            and (released := self._release()) is not None
        ):
            yield released
        if update is None:
            return
        # An update without contents is never held: the stream-idle watchdog
        # sees provider traffic while content waits.
        if update.contents and (index := self._held_index(event)) is not None:
            self._held.setdefault(index, []).append(update)
            if (evidence := self._hosted_evidence(update.contents)) is not None:
                yield evidence
        else:
            yield update

    def finish(self) -> None:
        """Check the stream said how the message ends; raise a retryable truncation when it did not."""
        if not self._ended:
            raise ProviderResponseError(
                "stream_truncated",
                "The stream ended before the message finished.",
                retryable=True,
                usage_details=self._usage,
            )

    def _release(self) -> ChatResponseUpdate | None:
        """Everything held, in block order, as one update; None when nothing is."""
        if not (self._tool_uses or self._held):
            return None
        if self._tool_uses and self._stop_reason == "refusal":
            # Hosted work held here was reported as it arrived.
            raise refused_calls_error(usage_details=self._usage)
        contents: list[Content] = []
        raws: list[Any] = []
        for index in sorted(self._tool_uses.keys() | self._held.keys()):
            if (tool_use := self._tool_uses.pop(index, None)) is not None:
                contents.append(tool_use.to_content())
            for update in self._held.pop(index, ()):
                contents.extend(update.contents)
                if update.raw_representation is not None:
                    raws.append(update.raw_representation)
        self._hold_from = None
        self._reported.clear()
        return ChatResponseUpdate(contents=contents, raw_representation=raws or None) if contents else None

    def _hosted_evidence(self, contents: list[Content]) -> ChatResponseUpdate | None:
        """Report held hosted work once per content: the retry gates count it before it is released."""
        fresh = [content for content in contents if content.provider_hosted and id(content) not in self._reported]
        if not fresh:
            return None
        self._reported.update(id(content) for content in fresh)
        return ChatResponseUpdate(contents=[], raw_representation=HeldHostedEvidence(tuple(fresh)))

    def _held_index(self, event: BetaRawMessageStreamEvent) -> int | None:
        """The block index under which *event*'s update is held; None when it is not held."""
        if (
            isinstance(event, BetaRawContentBlockStartEvent | BetaRawContentBlockDeltaEvent)
            and self._hold_from is not None
            and event.index >= self._hold_from
        ):
            return event.index
        return None

    def _take_usage(self, usage: UsageDetails) -> None:
        self._usage = normalize_stream_usage([self._usage or {}, usage])

    def _decode(self, event: Any) -> ChatResponseUpdate | None:
        match event.type:
            case "message_start":
                return self._message_start(event)
            case "message_delta":
                return self._message_delta(event)
            case "message_stop":
                self._ended = True
                self._hosted_blocks.clear()
                self._hosted_calls.clear()
                self._hosted_input.clear()
                logger.debug("Anthropic message_stop: the response is complete")
            case "content_block_start":
                return self._block_start(event)
            case "content_block_delta":
                return self._block_delta(event)
            case "content_block_stop":
                return self._block_stop(event)
            case _:
                logger.debug("Ignoring unsupported event type: %s", event.type)
        return None

    def _message_start(self, event: Any) -> ChatResponseUpdate:
        message = event.message
        log_input_transformations(message)
        self._first_cache_read = token_count(message.usage.cache_read_input_tokens if message.usage else None)
        contents = decode_blocks(message.content, origin=self._origin)
        if message.usage and (usage := decode_usage(message.usage)):
            self._take_usage(usage)
            contents.append(Content.from_usage(usage_details=usage))
        return ChatResponseUpdate(
            role="assistant",
            response_id=message.id,
            contents=contents,
            model=message.model,
            finish_reason=decode_stop_reason(message.stop_reason),
            raw_representation=event,
        )

    def _message_delta(self, event: Any) -> ChatResponseUpdate:
        # Only the last delta carries them, and only when another model took over mid-stream.
        log_input_transformations(event)
        usage = decode_usage(event.usage)
        if usage is not None:
            context_input = stream_context_input(event.usage, first_cache_read=self._first_cache_read)
            if context_input is not None:
                usage["context_input_token_count"] = context_input
            self._take_usage(usage)
        if stop_reason := event.delta.stop_reason:
            self._stop_reason = stop_reason
            self._ended = True
        return ChatResponseUpdate(
            contents=[Content.from_usage(usage_details=usage, raw_representation=event.usage)] if usage else [],
            finish_reason=decode_stop_reason(stop_reason),
            raw_representation=event,
        )

    def _block_start(self, event: Any) -> ChatResponseUpdate:
        block, index = event.content_block, event.index
        if block.type == "tool_use":
            if self._hold_from is None or index < self._hold_from:
                self._hold_from = index
            self._tool_uses[index] = _ToolUse(block.id, block.name, block.input, raw_parts=[block])
            return ChatResponseUpdate(contents=[], raw_representation=event)
        contents = decode_blocks([block], origin=self._origin)
        if block.type == "thinking":
            # Some gateways omit the signature on the start event. An empty
            # one, as the official start event carries, keeps this block from
            # merging into the signed thinking before it and losing its own
            # signature on replay.
            for content in contents:
                if content.type == "text_reasoning" and content.protected_data is None:
                    content.protected_data = ""
        if block.type in ("mcp_tool_use", "server_tool_use"):
            self._hosted_blocks.add(index)
        if index in self._hosted_blocks:
            call = next((content for content in contents if content.provider_hosted and content.call_id), None)
            if call is not None:
                self._hosted_calls[index] = call
                self._hosted_input[index] = []
        return ChatResponseUpdate(contents=contents, raw_representation=event)

    def _block_delta(self, event: Any) -> ChatResponseUpdate:
        delta, index = event.delta, event.index
        if delta.type != "input_json_delta":
            return ChatResponseUpdate(contents=decode_blocks([delta], origin=self._origin), raw_representation=event)
        if index in self._hosted_blocks:
            parts = self._hosted_input.setdefault(index, [])
            parts.append(delta.partial_json)
            if (call := self._hosted_calls.get(index)) is not None:
                # Re-emits the object the block start emitted, refreshed in
                # place: response assembly skips a content object it has
                # already seen, so the assembled message keeps this one call
                # with its latest input instead of one copy per fragment.
                raw_input = "".join(parts)
                try:
                    arguments = json.loads(raw_input)
                except json.JSONDecodeError:
                    arguments = raw_input
                apply_streamed_input(call, arguments)
                call.provider_phase = HostedToolPhase.DELTA
                call.raw_representation = event
                return ChatResponseUpdate(contents=[call], raw_representation=event)
        elif (tool_use := self._tool_uses.get(index)) is not None:
            tool_use.add_input(delta.partial_json, delta)
        else:
            logger.warning(
                "Ignoring Anthropic input_json_delta without a matching local tool-use block at index %d", index
            )
        return ChatResponseUpdate(contents=[], raw_representation=event)

    def _block_stop(self, event: Any) -> ChatResponseUpdate | None:
        index = event.index
        self._hosted_blocks.discard(index)
        if (call := self._hosted_calls.pop(index, None)) is not None:
            # The call is complete: back to the phase a blocking response
            # gives it. The assembled message holds this same object.
            call.provider_phase = HostedToolPhase.START
        self._hosted_input.pop(index, None)
        if index in self._tool_uses:
            # Stop events may interleave: a finished call stays held for the
            # release in block order.
            return ChatResponseUpdate(contents=[], raw_representation=event)
        logger.debug("Anthropic content_block_stop at index %d carries no content", index)
        return None
