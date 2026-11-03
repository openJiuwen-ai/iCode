# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Stream events read back as chat response updates.

One :class:`StreamState` reads one stream. Events about an output item name
it by its output index, so the state keeps one :class:`OutputSlot` per index:
the function call it assembles, the hosted call and result contents later
events update in place, and the text contents its message envelope belongs
to. An update carries what the event added; a hosted content already sent
is sent again whenever an event changes it.

A function call is sent once, whole, when its item is done. What comes
after a call not yet done, in output order, waits for it, so the contents
keep the order of the output; hosted work that waits is reported at once as
:class:`~chrys.foundation.hosted_tools.HeldHostedEvidence`. The terminal
event sends whatever still waits.

A stream that announced a response must end with its terminal event: a
failure (``response.failed``, an ``error`` event, a failed or cancelled
terminal response) raises, and so does a stream that ends before it
(:meth:`StreamState.finish`). A response that refused or was filtered yet
asks for function calls raises too, whenever the refusal came, a refusal
only a message snapshot shows included: the response lands only after the
stream ends, so its calls never run. One without calls whose refusal no text
showed ends as filtered (``content_filter``): it is refused, not blank. The
client reads nothing after the terminal event (:attr:`StreamState.ended`). A
stream that goes quiet before it is left to the stall watchdog, which sends
it again: unlike after a Chat Completions finish reason, nothing it said yet
tells how it ends.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from chrys.foundation.errors import ProviderResponseError, in_band_failure_retryable
from chrys.foundation.hosted_tools import (
    HeldHostedEvidence,
    HostedRetrySafety,
    HostedToolPhase,
)
from chrys.kernel import (
    OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY,
    Annotation,
    ChatResponseUpdate,
    Content,
    TextSpanRegion,
)
from chrys.kernel.exchanges import TOOL_RESULT_CONTENT_TYPES
from chrys.service.llm.reasoning_display import REASONING_SUMMARY_KEY
from chrys.service.llm.refusals import refused_calls_error
from chrys.service.profiles.models.options import effective_store_option

from .decode import (
    RUNNING_STATUSES,
    OpenAIContinuationToken,
    continuation_token,
    conversation_handle,
    decode_client_tool_call,
    decode_reasoning_item,
    decode_usage,
    finish_reason,
    hosted_contents,
    is_function_call,
    logprobs_metadata,
    output_message_envelope,
    refuses,
    response_failure,
    timestamp,
)
from .hosted import decode_hosted_item, image_data_uri, item_properties, refresh_in_place

if TYPE_CHECKING:
    from chrys.foundation.reasoning_origin import ReasoningOrigin
    from chrys.kernel import UsageDetails

    from .client import ResponsesVariant

logger = logging.getLogger(__name__)

# Progress events of hosted tools; the last segment is the new status.
_STATUS_EVENTS = (
    "response.web_search_call.in_progress",
    "response.web_search_call.searching",
    "response.web_search_call.completed",
    "response.file_search_call.in_progress",
    "response.file_search_call.searching",
    "response.file_search_call.completed",
    "response.mcp_call.in_progress",
    "response.mcp_call.completed",
    "response.mcp_call.failed",
    "response.code_interpreter_call.in_progress",
    "response.code_interpreter_call.interpreting",
    "response.code_interpreter_call.completed",
    "response.image_generation_call.in_progress",
    "response.image_generation_call.generating",
    "response.image_generation_call.completed",
)


@dataclass(slots=True)
class PendingCall:
    """A function call assembled from its events, held until its item is done."""

    item_id: str | None = None
    call_id: str | None = None
    name: str | None = None
    status: str | None = None
    # The arguments the added item came with, the deltas after it, and the
    # whole arguments a done event repeats, which win over the pieces.
    initial: str = ""
    deltas: list[str] = field(default_factory=list)
    final: str | None = None
    done: bool = False
    sent: bool = False
    raw: Any = None

    def learn(self, item: Any) -> None:
        """Take what an added or done item says about the call."""
        self.item_id = getattr(item, "id", None) or self.item_id
        self.call_id = getattr(item, "call_id", None) or self.call_id
        self.name = getattr(item, "name", None) or self.name
        self.status = getattr(item, "status", None) or self.status
        self.raw = item

    def content(self, output_index: Any) -> Content:
        arguments = self.final or ("".join(self.deltas) if self.deltas else self.initial)
        properties: dict[str, Any] = {"output_index": output_index, "fc_id": self.item_id}
        if self.status:
            properties["status"] = self.status
        return Content.from_function_call(
            call_id=self.call_id or "",
            name=self.name or "",
            arguments=arguments,
            additional_properties=properties,
            raw_representation=self.raw,
        )


@dataclass(slots=True)
class OutputSlot:
    """What the stream has seen of one output item."""

    function_call: PendingCall | None = None
    call: Content | None = None
    result: Content | None = None
    envelope: dict[str, str] | None = None
    message_contents: list[Content] = field(default_factory=list)

    def merge(self, snapshot: Content) -> Content:
        """Fold a whole-item snapshot into the content already sent for its side.

        The first snapshot becomes the carrier; later ones refresh it in
        place, so everything holding the carrier sees the newest state.
        """
        if snapshot.type in TOOL_RESULT_CONTENT_TYPES:
            self.result = _fold(self.result, snapshot)
            return self.result
        self.call = _fold(self.call, snapshot)
        return self.call


def _fold(carrier: Content | None, snapshot: Content) -> Content:
    if carrier is None:
        return snapshot
    refresh_in_place(carrier, snapshot)
    return carrier


@dataclass(slots=True)
class _Update:
    model: str
    contents: list[Content] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    conversation_id: str | None = None
    response_id: str | None = None
    created_at: str | None = None
    continuation_token: OpenAIContinuationToken | None = None
    finish_reason: Literal["length", "content_filter"] | None = None


class StreamState:
    """Reads the events of one stream, in order."""

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        model: str,
        variant: ResponsesVariant,
        origin: ReasoningOrigin | None = None,
    ) -> None:
        self._options = options
        self._model = model
        self._variant = variant
        # The endpoint that sends the stream, stamped on its reasoning.
        self._origin = origin
        self._slots: dict[Any, OutputSlot] = {}
        # Contents waiting behind a function call not yet done, by output
        # index, and the hosted ones among them not yet reported.
        self._held: dict[Any, list[Content]] = {}
        self._held_hosted: list[Content] = []
        # Reasoning items whose text came as deltas: their done event repeats it.
        self._reasoning_with_deltas: set[str] = set()
        # A lifecycle event announced the response; its terminal event came.
        self._announced = False
        self._ended = False
        # A non-empty refusal came; a function call came; message text that
        # is not blank went out.
        self._refused = False
        self._calls_seen = False
        self._text_sent = False
        # Local history replays reasoning by its encrypted payload, which may
        # only arrive once the item or the whole response is done: it then
        # goes onto the contents already sent for that item, by item id.
        self._backfills_reasoning = variant.encrypted_reasoning and effective_store_option(options) is False
        self._reasoning: dict[str, list[Content]] = {}

    @property
    def ended(self) -> bool:
        """Whether the terminal event came: the response ended."""
        return self._ended

    def updates_for(self, event: Any) -> list[ChatResponseUpdate]:
        """The updates one event makes: its own, after the evidence of hosted work it held."""
        update = self.update_for(event)
        if not self._held_hosted:
            return [update]
        evidence = ChatResponseUpdate(
            contents=[],
            role="assistant",
            model=self._model,
            raw_representation=HeldHostedEvidence(tuple(self._held_hosted)),
        )
        self._held_hosted.clear()
        return [evidence, update]

    def update_for(self, event: Any) -> ChatResponseUpdate:
        """The update one event makes; what it adds behind a call not yet done waits for that call."""
        update = _Update(model=self._model)
        handler = _HANDLERS.get(event.type)
        if handler is None:
            logger.debug("Unparsed event of type: %s: %s", event.type, event)
        else:
            handler(self, event, update)
        index = getattr(event, "output_index", None)
        if update.contents and _output_order(index)[0] == 0 and self._waits_behind_call(index):
            # A hosted content an event refreshes in place waits, and is
            # reported, once.
            held = self._held.setdefault(index, [])
            fresh = [content for content in update.contents if all(content is not other for other in held)]
            held.extend(fresh)
            self._held_hosted.extend(content for content in fresh if content.provider_hosted)
            update.contents = []
        self._release(update, drain=False)
        return ChatResponseUpdate(
            contents=update.contents,
            conversation_id=update.conversation_id,
            response_id=update.response_id,
            role="assistant",
            model=update.model,
            created_at=update.created_at,
            continuation_token=update.continuation_token,
            finish_reason=update.finish_reason,
            additional_properties=update.metadata,
            raw_representation=event,
        )

    def finish(self) -> ChatResponseUpdate | None:
        """What the stream still owes when it ends.

        A stream that announced its response but ended before the terminal
        event lost the rest of it: that is a truncation the next attempt
        may resume. A stream with no lifecycle events at all (some
        compatible endpoints send none) is kept, with a warning, and the
        calls it still holds are sent. A stream that refused yet asks for
        calls fails as filtered either way.
        """
        if self._ended:
            return None
        self._refuse_calls()
        if self._announced:
            raise ProviderResponseError(
                "stream_truncated", "The stream ended before the response finished.", retryable=True
            )
        logger.warning("Responses stream ended without a terminal event; the answer may be incomplete")
        update = _Update(model=self._model)
        self._release(update, drain=True)
        reason = self._finish_reason(None)
        if not update.contents and reason is None:
            return None
        return ChatResponseUpdate(contents=update.contents, role="assistant", model=update.model, finish_reason=reason)

    def _finish_reason(
        self, reason: Literal["length", "content_filter"] | None
    ) -> Literal["length", "content_filter"] | None:
        """*reason*, or ``content_filter`` for a refusal no text showed (a message snapshot's only).

        The response is not blank for want of an answer: it was refused, and
        sending it again meets the same refusal.
        """
        return "content_filter" if self._refused and not self._text_sent else reason

    def _slot(self, index: Any) -> OutputSlot:
        return self._slots.setdefault(index, OutputSlot())

    def _pending_call(self, index: Any) -> PendingCall:
        slot = self._slot(index)
        if slot.function_call is None:
            slot.function_call = PendingCall()
            self._calls_seen = True
        return slot.function_call

    def _waits_behind_call(self, index: Any) -> bool:
        """Whether a function call before *index* in output order is not sent yet."""
        return any(
            slot.function_call is not None
            and not slot.function_call.sent
            and _output_order(other) < _output_order(index)
            for other, slot in self._slots.items()
        )

    def _release(self, update: _Update, *, drain: bool) -> None:
        """Send what waits, in output order, up to the first call not done; with *drain*, all of it.

        A call waits for every call before it, so a later call that is done
        first does not overtake it, and neither does what follows either.
        """
        unsent = {index for index, slot in self._slots.items() if slot.function_call and not slot.function_call.sent}
        for index in sorted(unsent | self._held.keys(), key=_output_order):
            slot = self._slots.get(index)
            call = slot.function_call if slot is not None else None
            if call is not None and not call.sent:
                if not (drain or call.done):
                    return
                call.sent = True
                if call.call_id and call.name:
                    update.contents.append(call.content(index))
                else:
                    logger.warning("Responses stream dropped a function call without a call id or name at %r", index)
            update.contents.extend(self._held.pop(index, ()))

    def _unsent_hosted(self, response: Any) -> list[Content]:
        """The hosted work in a terminal response's output that the stream never sent, or still holds."""
        output = getattr(response, "output", None) or []
        unsent = [
            item
            for index, item in enumerate(output)
            if (slot := self._slots.get(index)) is None
            or (slot.call is None and slot.result is None)
            or index in self._held
        ]
        return hosted_contents(unsent, self._variant.hosted_provider)

    def _remember_reasoning(self, contents: list[Content]) -> None:
        """Stamp new reasoning contents with the endpoint, and keep them for a later payload."""
        for content in contents:
            if self._origin is not None:
                self._origin.stamp(content.additional_properties)
            if self._backfills_reasoning and content.id:
                self._reasoning.setdefault(content.id, []).append(content)

    def _backfill_reasoning(self, item: Any, *, final: bool) -> bool:
        """Put a reasoning item's payload on the last content sent for it; False when none was.

        A done item's payload is *final*; the terminal response's only fills
        a gap, never replacing a payload the contents already carry.
        """
        payload = getattr(item, "encrypted_content", None)
        item_id = getattr(item, "id", None)
        sent = self._reasoning.get(item_id) if isinstance(item_id, str) else None
        if not (payload and sent):
            return False
        if final or not any(content.protected_data for content in sent):
            sent[-1].protected_data = payload
        return True

    def _store(self) -> Any:
        return effective_store_option(self._options)

    # Text

    def _part_added(self, event: Any, update: _Update) -> None:
        part = event.part
        if part.type == "output_text":
            update.contents.append(self._message_text(event, part.text))
            update.metadata.update(logprobs_metadata(part))
        elif part.type == "refusal":
            self._refused = self._refused or bool(part.refusal)
            update.contents.append(self._message_text(event, part.refusal))

    def _text_delta(self, event: Any, update: _Update) -> None:
        update.contents.append(self._message_text(event, event.delta))
        update.metadata.update(logprobs_metadata(event))

    def _refusal_delta(self, event: Any, update: _Update) -> None:
        self._refused = self._refused or bool(event.delta)
        self._text_delta(event, update)

    def _refusal_done(self, event: Any, update: _Update) -> None:
        # Its text came as deltas.
        self._refused = self._refused or bool(getattr(event, "refusal", None))

    def _part_done(self, event: Any, update: _Update) -> None:
        # Its text came with the part or as deltas; a refusal may show only here.
        part = event.part
        if getattr(part, "type", None) == "refusal":
            self._refused = self._refused or bool(getattr(part, "refusal", None))

    def _annotation_added(self, event: Any, update: _Update) -> None:
        if (citation := streamed_citation(event)) is not None:
            update.contents.append(self._message_text(event, "", annotations=[citation]))

    def _message_text(self, event: Any, text: str, annotations: list[Annotation] | None = None) -> Content:
        """A text content of an output message, kept to receive its envelope when the item is done."""
        properties: dict[str, Any] = {}
        index = getattr(event, "output_index", None)
        tracked = isinstance(index, int) and not isinstance(index, bool)
        if tracked and (envelope := self._slot(index).envelope):
            properties[OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY] = dict(envelope)
        content = Content.from_text(
            text=text, annotations=annotations, raw_representation=event, additional_properties=properties or None
        )
        if tracked:
            self._slot(index).message_contents.append(content)
        self._text_sent = self._text_sent or bool(text.strip())
        return content

    # Reasoning

    def _reasoning_delta(self, event: Any, update: _Update) -> None:
        self._reasoning_with_deltas.add(event.item_id)
        content = _reasoning_text(event, event.delta)
        self._remember_reasoning([content])
        update.contents.append(content)
        update.metadata.update(logprobs_metadata(event))

    def _reasoning_done(self, event: Any, update: _Update) -> None:
        if event.item_id not in self._reasoning_with_deltas:
            content = _reasoning_text(event, event.text)
            self._remember_reasoning([content])
            update.contents.append(content)
        update.metadata.update(logprobs_metadata(event))

    # Response lifecycle

    def _started(self, event: Any, update: _Update) -> None:
        """``response.created`` or ``response.in_progress``: only a running response can be resumed."""
        self._announced = True
        response = event.response
        update.response_id = response.id
        update.conversation_id = conversation_handle(response, store=self._store(), variant=self._variant)
        if response.status in RUNNING_STATUSES:
            update.continuation_token = continuation_token(response.id, store=self._store(), variant=self._variant)

    def _finished(self, event: Any, update: _Update) -> None:
        """``response.completed`` or ``response.incomplete``: the response ends here, unless it failed or refused."""
        self._ended = True
        response = event.response
        usage = decode_usage(response.usage, variant=self._variant) if response.usage else None
        self._refuse_calls(response, usage)
        observed = self._unsent_hosted(response)
        if (failure := response_failure(response, observed=observed, usage_details=usage)) is not None:
            raise failure
        reason = finish_reason(response)
        output = getattr(response, "output", None) or []
        if self._backfills_reasoning:
            for item in output:
                if getattr(item, "type", None) == "reasoning":
                    self._backfill_reasoning(item, final=False)
        self._release(update, drain=True)
        update.response_id = response.id
        update.conversation_id = conversation_handle(response, store=self._store(), variant=self._variant)
        update.model = response.model
        update.created_at = timestamp(response.created_at)
        if usage:
            update.contents.append(Content.from_usage(usage_details=usage, raw_representation=event))
        update.finish_reason = self._finish_reason(reason)

    def _failed(self, event: Any, update: _Update) -> None:
        """``response.failed``: the error it carries, the hosted work it ran included."""
        self._ended = True
        response = event.response
        usage = decode_usage(response.usage, variant=self._variant) if response.usage else None
        self._refuse_calls(response, usage)
        observed = self._unsent_hosted(response)
        raise response_failure(response, observed=observed, usage_details=usage) or ProviderResponseError(
            "server_error",
            "The service reported the response as failed.",
            retryable=True,
            invalidates_continuation_token=True,
            observed_contents=tuple(observed),
            usage_details=usage,
        )

    def _error(self, event: Any, update: _Update) -> None:
        """An ``error`` event: the response ends with it."""
        self._ended = True
        self._refuse_calls()
        code = getattr(event, "code", None) or "server_error"
        raise ProviderResponseError(
            code,
            getattr(event, "message", None) or "The service reported an error.",
            retryable=in_band_failure_retryable(code),
            invalidates_continuation_token=True,
        )

    def failure(self) -> ProviderResponseError | None:
        """The failure the events so far decide, however the stream goes on: a refusal with calls, or None.

        The client asks here before reporting a stream that broke off.
        """
        return refused_calls_error() if self._refused and self._calls_seen else None

    def _refuse_calls(self, response: Any = None, usage: UsageDetails | None = None) -> None:
        """Fail a response that refused or was filtered yet asks for calls, however it ended.

        *response* is the terminal response, when the stream sent one; a
        call it lists counts even when no event streamed it.
        """
        if response is not None:
            output = getattr(response, "output", None) or []
            self._refused = self._refused or finish_reason(response) == "content_filter" or any(map(refuses, output))
            self._calls_seen = self._calls_seen or any(map(is_function_call, output))
        if self._refused and self._calls_seen:
            raise refused_calls_error(self._unsent_hosted(response), usage_details=usage)

    # Output items

    def _item_added(self, event: Any, update: _Update) -> None:
        item = event.item
        index = getattr(event, "output_index", -1)
        match item.type:
            case "message":
                # Its text comes as part events; a refusal may show only here.
                self._refused = self._refused or refuses(item)
                if envelope := output_message_envelope(item):
                    self._slot(index).envelope = envelope
            case "function_call":
                call = self._pending_call(index)
                call.learn(item)
                if isinstance(arguments := getattr(item, "arguments", None), str):
                    call.initial = arguments
            case "reasoning":
                contents = decode_reasoning_item(item, streamed=True)
                self._remember_reasoning(contents)
                update.contents.extend(contents)
            case "shell_call_output" | "tool_search_output":
                # Results are decoded once, from their done item.
                pass
            case _:
                if decoded := decode_hosted_item(item, self._variant.hosted_provider, phase=HostedToolPhase.START):
                    self._slot(index).call = decoded[0]
                    update.contents.append(decoded[0])

    def _item_done(self, event: Any, update: _Update) -> None:
        item = event.item
        index = getattr(event, "output_index", -1)
        slot = self._slot(index)
        match getattr(item, "type", None):
            case "function_call":
                call = self._pending_call(index)
                call.learn(item)
                if isinstance(arguments := getattr(item, "arguments", None), str) and arguments:
                    call.final = arguments
                call.done = True
            case "message":
                self._refused = self._refused or refuses(item)
                envelope = output_message_envelope(item)
                if envelope:
                    slot.envelope = envelope
                for content in slot.message_contents:
                    if envelope:
                        content.additional_properties[OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY] = dict(envelope)
                    else:
                        content.additional_properties.pop(OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY, None)
            case "reasoning":
                # The payload only arrives once the item is done.
                payload = getattr(item, "encrypted_content", None)
                if payload and not self._backfill_reasoning(item, final=True):
                    content = Content.from_text_reasoning(
                        id=getattr(item, "id", None), text="", protected_data=payload, raw_representation=item
                    )
                    self._remember_reasoning([content])
                    update.contents.append(content)
            case "custom_tool_call":
                name = getattr(item, "name", "") or ""
                update.contents.append(decode_client_tool_call(item, name=name, arguments=getattr(item, "input", None)))
            case "apply_patch_call":
                operation = getattr(item, "operation", None)
                update.contents.append(decode_client_tool_call(item, name="apply_patch", arguments=operation))
            case item_type:
                snapshots = decode_hosted_item(item, self._variant.hosted_provider) or []
                update.contents.extend(slot.merge(snapshot) for snapshot in snapshots)
                if item_type == "image_generation_call" and len(snapshots) == 1 and slot.result is not None:
                    # No final image: the result built from partial images ends here.
                    status = getattr(item, "status", None)
                    slot.result.status = status
                    slot.result.provider_status = status
                    slot.result.provider_phase = HostedToolPhase.TERMINAL
                    slot.result.additional_properties = item_properties(item, shadow=True)
                    slot.result.raw_representation = item
                    update.contents.append(slot.result)

    # Tool progress

    def _arguments_delta(self, event: Any, update: _Update) -> None:
        # Held until the item is done; the update still shows progress.
        call = self._pending_call(event.output_index)
        call.item_id = call.item_id or event.item_id
        call.deltas.append(event.delta)

    def _arguments_done(self, event: Any, update: _Update) -> None:
        call = self._pending_call(event.output_index)
        call.item_id = call.item_id or event.item_id
        if isinstance(arguments := getattr(event, "arguments", None), str) and arguments:
            call.final = arguments

    def _status_changed(self, event: Any, update: _Update) -> None:
        call = self._slot(getattr(event, "output_index", -1)).call
        if call is not None:
            status = event.type.rsplit(".", 1)[-1]
            call.status = status
            call.provider_status = status
            call.provider_phase = HostedToolPhase.SNAPSHOT
            call.raw_representation = event
            update.contents.append(call)

    def _code_delta(self, event: Any, update: _Update) -> None:
        properties = _code_properties(event)
        slot = self._slot(event.output_index)
        call = slot.call
        if call is None:
            call = slot.call = self._code_call(event)
            update.contents.append(call)
        if not call.inputs:
            call.inputs = [Content.from_text(text="")]
        code = call.inputs[0]
        code.text = (code.text or "") + event.delta
        code.raw_representation = event
        code.additional_properties = properties
        call.provider_phase = HostedToolPhase.DELTA
        call.raw_representation = event
        if call not in update.contents:
            update.contents.append(call)
        update.metadata.update(logprobs_metadata(event))

    def _code_done(self, event: Any, update: _Update) -> None:
        properties = _code_properties(event)
        slot = self._slot(event.output_index)
        call = slot.call
        if call is None:
            call = slot.call = self._code_call(event)
        call.inputs = [Content.from_text(text=event.code, raw_representation=event, additional_properties=properties)]
        call.provider_phase = HostedToolPhase.SNAPSHOT
        call.raw_representation = event
        update.contents.append(call)
        update.metadata.update(logprobs_metadata(event))

    def _code_call(self, event: Any) -> Content:
        """The code call for code that arrives before its item."""
        return Content.from_code_interpreter_tool_call(
            call_id=getattr(event, "call_id", None) or getattr(event, "id", None) or event.item_id,
            inputs=[],
            hosted_provider=self._variant.hosted_provider,
            provider_item_type="code_interpreter_call",
            provider_item_id=event.item_id,
            retry_safety=HostedRetrySafety.SANDBOXED,
        )

    def _partial_image(self, event: Any, update: _Update) -> None:
        """A partial image, added to a result that stays open until the item is done."""
        image = Content.from_uri(
            uri=image_data_uri(event.partial_image_b64),
            additional_properties={"partial_image_index": event.partial_image_index, "is_partial_image": True},
            raw_representation=event,
        )
        image_id = getattr(event, "item_id", None)
        slot = self._slot(getattr(event, "output_index", -1))
        provider = self._variant.hosted_provider
        if slot.call is None:
            slot.call = Content.from_image_generation_tool_call(
                image_id=image_id,
                hosted_provider=provider,
                provider_item_type="image_generation_call",
                provider_item_id=image_id,
                provider_phase=HostedToolPhase.START,
                retry_safety=HostedRetrySafety.SANDBOXED,
                raw_representation=event,
            )
            update.contents.append(slot.call)
        if slot.result is None:
            slot.result = Content.from_image_generation_tool_result(
                image_id=image_id,
                outputs=[],
                hosted_provider=provider,
                provider_item_type="image_generation_call",
                provider_item_id=image_id,
                provider_phase=HostedToolPhase.SNAPSHOT,
                provider_status="generating",
                retry_safety=HostedRetrySafety.SANDBOXED,
                raw_representation=event,
            )
        result = slot.result
        if not isinstance(result.outputs, list):
            result.outputs = []
        result.outputs.append(image)
        result.provider_phase = HostedToolPhase.SNAPSHOT
        result.provider_status = "generating"
        result.raw_representation = event
        update.contents.append(result)


def _output_order(index: Any) -> tuple[int, int]:
    """Output indexes in order; a missing one after them."""
    if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
        return (0, index)
    return (1, 0)


def _reasoning_text(event: Any, text: str) -> Content:
    is_text = event.type.startswith("response.reasoning_text.")
    properties: dict[str, Any] = {"reasoning_text": True} if is_text else {REASONING_SUMMARY_KEY: True}
    return Content.from_text_reasoning(
        id=event.item_id, text=text, raw_representation=event, additional_properties=properties
    )


def _code_properties(event: Any) -> dict[str, Any]:
    return {"output_index": event.output_index, "sequence_number": event.sequence_number, "item_id": event.item_id}


def streamed_citation(event: Any) -> Annotation | None:
    """The citation an ``output_text.annotation.added`` event adds, if it names its source.

    The annotation may be a mapping or an object. Blocking responses
    describe citations in another shape (:func:`.decode._citation`).
    """
    annotation = event.annotation

    def value(key: str) -> Any:
        if isinstance(annotation, dict):
            return annotation.get(key)
        return getattr(annotation, key, None)

    kind = value("type")
    file_id = value("file_id")
    if kind == "file_path":
        if not file_id:
            return None
        return Annotation(
            type="citation",
            file_id=str(file_id),
            additional_properties={"annotation_index": event.annotation_index, "index": value("index")},
            raw_representation=annotation,
        )
    if kind == "file_citation":
        if not file_id:
            return None
        return Annotation(
            type="citation",
            file_id=str(file_id),
            url=value("filename"),
            additional_properties={"annotation_index": event.annotation_index, "index": value("index")},
            raw_representation=annotation,
        )
    if kind == "container_file_citation":
        if not file_id:
            return None
        citation = Annotation(
            type="citation",
            file_id=str(file_id),
            url=value("filename"),
            additional_properties={"annotation_index": event.annotation_index, "container_id": value("container_id")},
            raw_representation=annotation,
        )
    elif kind == "url_citation":
        url = value("url")
        if not url:
            return None
        properties: dict[str, Any] = {"annotation_index": event.annotation_index}
        if (get_url := value("get_url")) is not None:
            properties["get_url"] = get_url
        citation = Annotation(
            type="citation",
            title=value("title") or "",
            url=str(url),
            additional_properties=properties,
            raw_representation=annotation,
        )
    else:
        logger.debug("Unparsed annotation type in streaming: %s", kind)
        return None
    start, end = value("start_index"), value("end_index")
    if start is not None and end is not None:
        citation["annotated_regions"] = [TextSpanRegion(type="text_span", start_index=start, end_index=end)]
    return citation


_HANDLERS: dict[str, Callable[[StreamState, Any, _Update], None]] = {
    "response.content_part.added": StreamState._part_added,
    "response.content_part.done": StreamState._part_done,
    "response.output_text.delta": StreamState._text_delta,
    "response.refusal.delta": StreamState._refusal_delta,
    "response.refusal.done": StreamState._refusal_done,
    "response.output_text.annotation.added": StreamState._annotation_added,
    "response.reasoning_text.delta": StreamState._reasoning_delta,
    "response.reasoning_summary_text.delta": StreamState._reasoning_delta,
    "response.reasoning_text.done": StreamState._reasoning_done,
    "response.reasoning_summary_text.done": StreamState._reasoning_done,
    "response.created": StreamState._started,
    "response.in_progress": StreamState._started,
    "response.completed": StreamState._finished,
    "response.incomplete": StreamState._finished,
    "response.failed": StreamState._failed,
    "error": StreamState._error,
    "response.output_item.added": StreamState._item_added,
    "response.output_item.done": StreamState._item_done,
    "response.function_call_arguments.delta": StreamState._arguments_delta,
    "response.function_call_arguments.done": StreamState._arguments_done,
    "response.code_interpreter_call_code.delta": StreamState._code_delta,
    "response.code_interpreter_call_code.done": StreamState._code_done,
    "response.image_generation_call.partial_image": StreamState._partial_image,
    **dict.fromkeys(_STATUS_EVENTS, StreamState._status_changed),
}
