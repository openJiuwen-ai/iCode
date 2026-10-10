# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Streamed chunks read back as chat response updates.

Tool calls arrive in fragments keyed by an index and, usually, an id. One
:class:`StreamState` per stream assembles them per choice and releases each
call once, whole, in an update of its own just before the update that
finishes its choice, or at the end of a stream no chunk finished. The kernel
never sees a fragment.

Some gateways end a stream without a finish reason. Its calls are released
only when every one of them has arguments a model could have finished. A
stream that refused or was filtered fails instead of handing over calls,
also when the refusal comes after calls it already released: the response
lands only after the stream ends, so its calls never run.

A choice finishes once: a finish reason that follows on it counts only when
it reports the content filter, which still refuses the calls released. A
stream has said how it ends once every choice finished or it settled on a
failure (:attr:`StreamState.ended`): the client reads what follows up to its
usage, waiting a short bound for anything that adds to the answer, for the
usage and for a refusal or failure it still reports.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from openai.types import CompletionUsage
from openai.types.chat.chat_completion_message_custom_tool_call import ChatCompletionMessageCustomToolCall
from pydantic import ValidationError

from chrys.foundation.errors import ProviderResponseError
from chrys.kernel import (
    CONTEXT_WINDOW_FILLED_KEY,
    ChatResponseUpdate,
    Content,
    FinishReason,
    UsageDetails,
    normalize_stream_usage,
)
from chrys.kernel.exceptions import ChatClientInvalidResponseException
from chrys.service.llm.openai_timestamps import normalize_openai_created_payload, openai_created_at_iso

from .decode import (
    choice_metadata,
    decode_usage,
    filled_window,
    finish_failure,
    finish_reason,
    has_answer,
    has_refusal,
    refused_calls_error,
    response_metadata,
    text_contents,
)
from .reasoning import REASONING_DETAILS_FIELD, REASONING_FORMAT_KEY, delta_reasoning, reasoning_fields
from .validation import raise_invalid_stream_event

if TYPE_CHECKING:
    from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
    from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice

    from chrys.foundation.reasoning_origin import ReasoningOrigin

    from .client import ChatCompletionsVariant

logger = logging.getLogger(__name__)


@dataclass
class _PendingCall:
    """One function call assembled from its fragments."""

    choice: int
    index: int | None
    # Position among all calls of the stream, in the order first seen.
    order: int
    call_id: str | None = None
    name: str | None = None
    arguments: list[str] = field(default_factory=list)
    raws: list[Any] = field(default_factory=list)

    def content(self) -> Content:
        return Content.from_function_call(
            call_id=self.call_id or "",
            name=self.name or "",
            arguments="".join(self.arguments),
            raw_representation=self.raws[0] if len(self.raws) == 1 else self.raws,
        )


@dataclass
class _ChoiceState:
    """What one choice of the stream has assembled."""

    calls: list[_PendingCall] = field(default_factory=list)
    by_index: dict[int, _PendingCall] = field(default_factory=dict)
    by_id: dict[str, _PendingCall] = field(default_factory=dict)
    # An empty plaintext reasoning field was reported once.
    empty_reasoning_sent: bool = False
    # Non-empty plaintext reasoning arrived.
    reasoning_seen: bool = False
    # Answer text that is not blank, a refusal or a call to a named function
    # arrived (``has_answer``).
    answered: bool = False
    # The choice's finish reason arrived; later fragments are ignored.
    finished: bool = False


class StreamState:
    """The assembly state of one stream."""

    def __init__(self, variant: ChatCompletionsVariant, *, origin: ReasoningOrigin | None = None) -> None:
        self._variant = variant
        # The endpoint that sends the stream, stamped on its reasoning_details.
        self._origin = origin
        self._choices: dict[int, _ChoiceState] = {}
        self._next_order = 0
        # A choice finished with a finish reason.
        self._finish_seen = False
        # The stream refused (an explicit refusal) or was filtered.
        self._refused = False
        # Calls went out in an update.
        self._calls_released = False
        # The usage the stream reported so far, the latest value of each
        # count, which a failure's error carries: the usage often comes in a
        # chunk of its own.
        self._usage: UsageDetails | None = None
        # The finish reasons of the chunk that reported a failure.
        self._failed: tuple[str, ...] | None = None
        # The stream reported how it ends: a failure finish reason, or a
        # refusal with calls. It yields nothing more; what follows is read
        # only for its usage.
        self._settled = False
        # The first id a chunk carried, which every update reports as its
        # response and message id. A stream holds one completion under one id,
        # but some gateways send a fresh id with each chunk: under each chunk's
        # own id the reply would split into a message per chunk, and the
        # closing usage chunk's empty message would end it as a blank reply.
        self._id: str | None = None

    @property
    def ended(self) -> bool:
        """Whether the stream said how it ends.

        Every choice finished, or it settled: a failure finish reason, or a
        refusal with calls.
        """
        return self._settled or (self._finish_seen and all(state.finished for state in self._choices.values()))

    def updates_for(self, chunk: ChatCompletionChunk) -> list[ChatResponseUpdate]:
        """The updates one chunk yields: calls its finish reasons complete, then its own.

        A failure finish reason, or a refusal with calls, settles the stream:
        it yields nothing more. Its error is raised at the first usage report
        from that chunk on, or when the stream ends: the usage commonly comes
        in a chunk of its own after the finish reason. See :meth:`failure`.
        """
        choices = getattr(chunk, "choices", None)
        if not isinstance(choices, list):
            raise_invalid_stream_event(chunk, choices)
        if not choices and chunk.usage is None:
            return []
        if self._settled:
            if self._take_usage(chunk) is not None:
                self._raise_failure()
            return []
        # Read once: the update reports these and the release keys on them.
        finished = self._finish_reasons(chunk)
        update = self.update_for(chunk, finished)
        if finish_failure(finished.values()) is not None:
            self._failed = tuple(finished.values())
            self._settled = True
        # A consumer may stop at a finish reason, so the calls go out first.
        calls = self._release(finished) if finished and not self._settled else None
        if self._refused and self._calls_released:
            # A refusal may follow calls an earlier choice finished with.
            # Not while a call is still pending, on purpose: settling then
            # would hold the rest of the reply to the tail's bound and could
            # lose the usage it reports. A stream that then ends or breaks off
            # still raises the refusal (:meth:`failure`); only one that goes
            # quiet before any finish reason is left to the stall watchdog,
            # which sends the request again.
            self._settled = True
        if self._settled:
            if any(content.type == "usage" for content in update.contents):
                self._raise_failure()
            return []
        return [update] if calls is None else [calls, update]

    def failure(self) -> ProviderResponseError | None:
        """The failure the stream reported so far, with the usage so far, or None.

        A refusal or filter with tool calls outranks a failure finish reason.
        Either outranks how the stream ends, also when it breaks off or goes
        quiet: the client asks here before reporting either.
        """
        if self._refused and (self._calls_released or self._calls_pending()):
            return refused_calls_error(usage_details=self._usage)
        if self._failed is not None:
            return finish_failure(self._failed, usage_details=self._usage)
        return None

    def finish(self, *, requires_finish_reason: bool = False) -> ChatResponseUpdate | None:
        """The calls still pending at the end of the stream.

        A failure the stream reported is raised first. Every pending call is
        checked before any is released: a call whose arguments are not a
        JSON object (or empty) was cut off, and the whole response is then a
        truncated one. A stream that ends with no call and no finish reason
        may have lost its answer's end; that fails under
        *requires_finish_reason* and is logged otherwise.
        """
        self._raise_failure()
        if self._calls_pending():
            return self._release(None)
        if not self._finish_seen:
            if requires_finish_reason:
                raise ProviderResponseError(
                    "stream_truncated",
                    "The stream ended without a finish reason.",
                    retryable=True,
                    usage_details=self._usage,
                )
            logger.warning("Chat Completions stream ended without a finish reason; the answer may be incomplete")
        return None

    def update_for(self, chunk: ChatCompletionChunk, finished: Mapping[int, str] | None = None) -> ChatResponseUpdate:
        """The update for one chunk, its call fragments held back.

        *finished* holds the chunk's finish reasons by choice, as
        :meth:`updates_for` read them; they are read here when not given.
        A ``created`` in milliseconds is converted on a copy, which is also the
        update's raw representation; the SDK chunk stays as received. The
        update carries the first id the stream's chunks carried.
        """
        if finished is None:
            finished = self._finish_reasons(chunk)
        chunk = normalize_openai_created_payload(chunk)
        if self._id is None and chunk.id:
            self._id = chunk.id
        metadata = response_metadata(chunk)
        contents: list[Content] = []
        finish: FinishReason | None = None
        # Usage can share a chunk with content (Gemini); both are kept.
        if (details := self._take_usage(chunk)) is not None:
            contents.append(Content.from_usage(usage_details=details, raw_representation=chunk))
        for choice in chunk.choices:
            metadata.update(choice_metadata(choice))
            if (reason := finished.get(choice.index)) is not None:
                finish = FinishReason(reason)
            if filled_window(choice.finish_reason, reason):
                metadata[CONTEXT_WINDOW_FILLED_KEY] = True
            # Some compatible providers finish with ``"delta": null``.
            if choice.delta is not None:
                contents.extend(self._delta_contents(choice))
        return ChatResponseUpdate(
            contents=contents,
            role="assistant",
            response_id=self._id,
            message_id=self._id,
            model=chunk.model,
            created_at=openai_created_at_iso(chunk.created),
            finish_reason=finish,
            additional_properties=metadata,
            raw_representation=chunk,
        )

    def _raise_failure(self) -> None:
        """Raise the failure the stream reported so far, if any."""
        if (failure := self.failure()) is not None:
            raise failure

    def _finish_reasons(self, chunk: ChatCompletionChunk) -> dict[int, str]:
        """The finish reasons of the chunk's choices that report one, read as ``finish_reason`` reads them.

        A choice that already finished keeps its answer: only a content filter
        that follows still counts.
        """
        reasons: dict[int, str] = {}
        for choice in chunk.choices:
            state = self._choices.get(choice.index)
            answered = (state is not None and state.answered) or has_answer(choice.delta)
            if (reason := finish_reason(choice.finish_reason, answered=answered)) is None:
                continue
            if state is None or not state.finished or reason == "content_filter":
                reasons[choice.index] = reason
        return reasons

    def _take_usage(self, chunk: ChatCompletionChunk) -> UsageDetails | None:
        """The usage the chunk reports, also taken into the usage so far."""
        if not (usage := chunk.usage or _choice_level_usage(chunk)):
            return None
        details = decode_usage(usage, variant=self._variant)
        self._usage = normalize_stream_usage([self._usage or {}, details])
        return details

    def _choice(self, index: int) -> _ChoiceState:
        return self._choices.setdefault(index, _ChoiceState())

    def _calls_pending(self) -> bool:
        return any(state.calls for state in self._choices.values())

    def _delta_contents(self, choice: ChunkChoice) -> list[Content]:
        state = self._choice(choice.index)
        state.answered = state.answered or has_answer(choice.delta)
        if has_refusal(choice.delta):
            self._refused = True
        fields = reasoning_fields(choice.delta)
        plain = next(((name, value) for name, value in fields.items() if name != REASONING_DETAILS_FIELD), None)
        contents: list[Content] = []
        include_plain = True
        if plain is not None and plain[1] == "":
            include_plain = False
            if not (state.empty_reasoning_sent or state.reasoning_seen):
                # The field's presence is kept once, ahead of answer text and
                # of the calls released at the finish: the text does not
                # fragment, and a call cut off at the length limit stays the
                # response's last content.
                contents.append(
                    Content.from_text_reasoning(
                        text="", additional_properties={REASONING_FORMAT_KEY: plain[0]}, raw_representation=choice.delta
                    )
                )
                state.empty_reasoning_sent = True
        elif plain is not None:
            state.reasoning_seen = True
        if choice.delta.tool_calls:
            self._collect_calls(choice.index, state, choice.delta.tool_calls)
        # A delta's fields carry no order among themselves: one that mixes
        # them reads as reasoning ending and the answer starting, so chunking
        # alone never moves reasoning into the answer text.
        contents.extend(delta_reasoning(fields, include_plain=include_plain, origin=self._origin))
        contents.extend(text_contents(choice))
        return contents

    def _collect_calls(self, choice: int, state: _ChoiceState, fragments: list[Any]) -> None:
        """Add call fragments to the calls they continue.

        A non-empty id names its call, as some gateways send several whole
        calls under one index. A fragment under an id not seen yet still
        continues the call its index holds when it names no function, or when
        that call has neither a name nor arguments yet, as some gateways change
        the id mid-call: the call keeps its first id and answers to both. A
        fragment without an id continues the call its index last named, or the
        only pending call when that is unambiguous.
        """
        if state.finished:
            logger.warning(
                "Ignoring streamed tool-call fragment received after the terminal update for choice %d", choice
            )
            return
        for fragment in fragments:
            if isinstance(fragment, ChatCompletionMessageCustomToolCall):
                continue
            index = _fragment_index(fragment)
            call_id = _fragment_call_id(fragment)
            function = getattr(fragment, "function", None)
            name = getattr(function, "name", None)
            named = isinstance(name, str) and bool(name)
            if call_id is None:
                pending = self._call_without_id(choice, state, index)
            else:
                pending = self._call_with_id(choice, state, call_id, index, named=named)
            if index is not None:
                # The id wins over the index: Gemini-compatible endpoints
                # reuse index 0 for distinct whole calls.
                state.by_index[index] = pending
                if pending.index is None:
                    pending.index = index
            if named:
                if pending.name is not None and pending.name != name:
                    raise ChatClientInvalidResponseException(
                        f"Conflicting streamed tool-call names for choice {choice}, index {index!r}."
                    )
                pending.name = name
            if (arguments := getattr(function, "arguments", None)) is not None:
                if not isinstance(arguments, str):
                    raise ChatClientInvalidResponseException(
                        f"Non-string streamed tool-call arguments for choice {choice}, index {index!r}."
                    )
                pending.arguments.append(arguments)
            pending.raws.append(fragment if function is None else function)

    def _call_with_id(
        self, choice: int, state: _ChoiceState, call_id: str, index: int | None, *, named: bool
    ) -> _PendingCall:
        if (known := state.by_id.get(call_id)) is not None:
            return known
        indexed = state.by_index.get(index) if index is not None else None
        if indexed is not None and indexed.call_id is None:
            # Some endpoints send the id only after a call's first fragment.
            pending = indexed
        elif indexed is not None and not (named and (indexed.name or any(indexed.arguments))):
            # Some endpoints mint a new id for each fragment of one call, and
            # may send the name only after a first fragment that carries
            # nothing else. The call keeps its first id; the new one only finds
            # it again. A fragment that names a function under a new id starts
            # a call when the call its index holds has a name or arguments: a
            # whole call, even one sent without its name, which stays apart for
            # response validation.
            logger.debug(
                "Streamed tool-call fragment for choice %d, index %r changed its call id; continuing the call",
                choice,
                index,
            )
            state.by_id[call_id] = indexed
            return indexed
        elif index is None and len(state.calls) == 1 and state.calls[0].call_id is None:
            # The same, with neither fragment carrying an index.
            pending = state.calls[0]
        else:
            pending = self._new_call(choice, state, index)
        pending.call_id = call_id
        state.by_id[call_id] = pending
        return pending

    def _call_without_id(self, choice: int, state: _ChoiceState, index: int | None) -> _PendingCall:
        if index is not None and (indexed := state.by_index.get(index)) is not None:
            return indexed
        only = state.calls[0] if len(state.calls) == 1 else None
        if only is not None and (index is None or only.index is None):
            return only
        if index is not None or not state.calls:
            return self._new_call(choice, state, index)
        raise ChatClientInvalidResponseException(
            f"Ambiguous streamed tool-call fragment without a call id for choice {choice}, index {index!r}."
        )

    def _new_call(self, choice: int, state: _ChoiceState, index: int | None) -> _PendingCall:
        pending = _PendingCall(choice=choice, index=index, order=self._next_order)
        self._next_order += 1
        state.calls.append(pending)
        if index is not None:
            state.by_index[index] = pending
        return pending

    def _release(self, finished: Mapping[int, str] | None) -> ChatResponseUpdate | None:
        """One update with the complete calls of the finished choices, or of every choice at the end."""
        chosen = sorted(self._choices if finished is None else finished)
        if finished is not None:
            self._finish_seen = True
            self._refused = self._refused or "content_filter" in finished.values()
        pending = [call for choice in chosen if (state := self._choices.get(choice)) for call in state.calls]
        # A nameless call in a choice cut off at the length limit is dropped
        # (``_complete_calls``), so it cannot turn a refusal into a failure.
        released = [call for call in pending if call.name or finished is None or finished.get(call.choice) != "length"]
        if released and self._refused:
            # The calls never go out: the stream settles on the refusal,
            # raised once its usage is in (``failure``).
            self._settled = True
            return None
        if finished is None and any(not _arguments_complete(call) for call in pending):
            raise ProviderResponseError(
                "stream_truncated",
                "The stream ended without a finish reason while a tool call's arguments were incomplete.",
                retryable=True,
                usage_details=self._usage,
            )
        contents: list[Content] = []
        for choice in chosen:
            state = self._choices.get(choice)
            if state is None:
                if finished is not None:
                    # A choice finished (``delta: null``) before sending
                    # anything. Remembering it keeps a late fragment from
                    # starting a call the end of the stream would release.
                    self._choice(choice).finished = True
                continue
            contents.extend(_complete_calls(choice, state.calls, None if finished is None else finished.get(choice)))
            state.calls.clear()
            state.by_index.clear()
            state.by_id.clear()
            if finished is not None:
                state.finished = True
        if not contents:
            return None
        self._calls_released = True
        return ChatResponseUpdate(contents=contents)


def _complete_calls(choice: int, calls: list[_PendingCall], finish_reason: str | None) -> list[Content]:
    """The calls of one choice as contents, in index order.

    A call without an id keeps the kernel's id-less behavior. A call without a
    name is handed over as sent, for response validation to retry the request;
    in a choice cut off at the length limit it is dropped: the model never
    finished it, and the finish reason already says why.
    """
    ordered = sorted(calls, key=lambda call: (call.order if call.index is None else call.index, call.order))
    if len(ordered) > 1:
        _assign_local_ids(choice, ordered)
    contents: list[Content] = []
    for call in ordered:
        if not call.name and finish_reason == "length":
            logger.warning(
                "Discarding truncated streamed tool call without a name for choice %d, index %r", choice, call.index
            )
            continue
        if not call.name:
            logger.warning(
                "Streamed tool call ended without a function name for choice %d, index %r", choice, call.index
            )
        if not call.call_id:
            logger.debug(
                "OpenAI-compatible stream emitted a tool call without an id for choice %d, index %r; "
                "preserving legacy id-less behavior",
                choice,
                call.index,
            )
        contents.append(call.content())
    return contents


def _arguments_complete(call: _PendingCall) -> bool:
    """Whether a call's arguments could be whole: none at all, or a JSON object.

    Only the shape is checked; a JSON object proves no more than that the
    text was not cut inside it.
    """
    arguments = "".join(call.arguments)
    if not arguments.strip():
        return True
    try:
        return isinstance(json.loads(arguments), dict)
    except ValueError:
        return False


def _assign_local_ids(choice: int, calls: list[_PendingCall]) -> None:
    """Give the id-less calls of a parallel batch request-local ids.

    The kernel merges adjacent fragments that share an id, so two id-less
    calls would fuse; distinct ids keep them apart and pair each with its
    result.
    """
    taken = {call.call_id for call in calls if call.call_id}
    for call in calls:
        if call.call_id:
            continue
        base = f"call_chrys_{choice}_{call.order}"
        candidate, suffix = base, 1
        while candidate in taken:
            candidate = f"{base}_{suffix}"
            suffix += 1
        call.call_id = candidate
        taken.add(candidate)
        logger.debug("Assigned a local id to an id-less parallel tool call for choice %d, index %r", choice, call.index)


def _fragment_index(fragment: Any) -> int | None:
    """The fragment's index when usable; the SDK does not validate it on construction."""
    index = getattr(fragment, "index", None)
    return index if isinstance(index, int) and not isinstance(index, bool) and index >= 0 else None


def _fragment_call_id(fragment: Any) -> str | None:
    """The fragment's call id; blank and a stringified JSON ``null`` count as none."""
    call_id = getattr(fragment, "id", None)
    return call_id if isinstance(call_id, str) and call_id not in ("", "null") else None


def _choice_level_usage(chunk: ChatCompletionChunk) -> CompletionUsage | None:
    """Usage a provider (Kimi) streams on a choice instead of on the chunk."""
    for choice in chunk.choices:
        if raw := (choice.model_extra or {}).get("usage"):
            try:
                return CompletionUsage.model_validate(raw)
            except ValidationError:
                logger.debug("Ignoring malformed choice-level usage on stream chunk %s", chunk.id)
    return None
