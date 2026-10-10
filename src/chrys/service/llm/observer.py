# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a wire client reports about each request it sends.

A :class:`WireCallObserver` belongs to one wire client and sees every request
that client sends (one *acquisition*). For each acquisition it:

- reports the request as one exchange of the active trajectory trace
  (:class:`ExchangeRecorder`): the start marker before the request leaves,
  the finish marker once the response lands;
- stamps the measured response span on the landed messages and on the
  provider-hosted contents the provider newly produced, leaving any message
  or content the provider echoed from the request untouched; a continued or
  polled background response therefore records the latency of its last poll
  alone;
- scopes the request's HTTP attempts (``request_tracking.py``): records the
  last attempt's id and the provider's request id on the response, and stamps
  that attempt id on the messages the provider newly returned (never on
  contents, and never on echoed messages);
- hands the text the model wrote beside its tool calls to the intermediate-text
  callback before the tool loop sees the response, so the UI can show it ahead
  of the tool calls. Non-streaming responses go to the async callback, awaited
  before the response is returned; streams go to the sync callback from a
  result hook, which runs when the stream finalizes and before the tool loop
  inspects the response. The streaming callback typically stores the text in
  an ``IntermediateTextBuffer`` that is released before the next
  ``ToolCallStart`` (or, for a call that starts no tool, the next response or
  the pass end).

Internal side calls (the LAST_WORDS completer, below ``get_response``) never
join the conversation: they keep their exchange report but get neither timing
nor intermediate text.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from chrys.foundation.trajectory.context import ExchangeTrace, TrajectoryContext, current_trajectory
from chrys.foundation.trajectory.envelope import ActorKind
from chrys.foundation.trajectory.event_types import ContinuationMode, ExchangeOutcome
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.usage import normalized_usage, provider_reported_usage, usage_measurements
from chrys.foundation.trajectory_timing import build_trajectory_timing, stamp_trajectory_timing
from chrys.kernel import in_internal_side_call
from chrys.kernel.instrumentation import _stream_abandoned, _stream_error_of
from chrys.kernel.types import ChatResponse, Message, ResponseStream
from chrys.service.agent_middleware.events.intermediate_text import intermediate_text_contents
from chrys.service.llm.request_tracking import REQUEST_ATTEMPT_ID_METADATA, RequestTracking
from chrys.service.session.message_metadata import stamp_message_response_timing
from chrys.service.trajectory.revisions import record_context_revision

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from chrys.kernel.types import ChatResponseUpdate


@dataclass(frozen=True, slots=True)
class _RequestEchoAnchors:
    """Request-scoped strong anchors plus O(1) identity membership indexes."""

    message_metadata: tuple[object, ...]
    contents: tuple[object, ...]
    content_metadata: tuple[object, ...]
    message_metadata_ids: frozenset[int]
    content_ids: frozenset[int]
    content_metadata_ids: frozenset[int]

    @classmethod
    def from_messages(cls, messages: Sequence[Message]) -> _RequestEchoAnchors:
        message_metadata = tuple(message.additional_properties for message in messages)
        contents = tuple(content for message in messages for content in message.contents)
        content_metadata = tuple(content.additional_properties for content in contents)
        return cls(
            message_metadata=message_metadata,
            contents=contents,
            content_metadata=content_metadata,
            message_metadata_ids=frozenset(id(metadata) for metadata in message_metadata),
            content_ids=frozenset(id(content) for content in contents),
            content_metadata_ids=frozenset(id(metadata) for metadata in content_metadata),
        )


@dataclass(frozen=True, slots=True)
class _ResponseTiming:
    """When one wire request left, and which request objects a response may echo."""

    started_at: datetime
    started_monotonic: float
    echo_anchors: _RequestEchoAnchors

    @classmethod
    def start(cls, messages: Sequence[Message]) -> _ResponseTiming:
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        return cls(started_at, started_monotonic, _RequestEchoAnchors.from_messages(messages))

    def stamp(self, response: ChatResponse[Any]) -> None:
        """Attach one measured provider-response span without mutating request echoes."""
        finished_at = datetime.now(UTC)
        timing = build_trajectory_timing(
            started_at=self.started_at,
            finished_at=finished_at,
            duration_ms=int((time.monotonic() - self.started_monotonic) * 1000),
        )
        anchors = self.echo_anchors
        for message in response.messages:
            if id(message.additional_properties) not in anchors.message_metadata_ids:
                stamp_message_response_timing(
                    message,
                    started_at=self.started_at,
                    finished_at=finished_at,
                    duration_ms=timing["duration_ms"],
                )
            for content in message.contents:
                if (
                    content.provider_hosted
                    and id(content) not in anchors.content_ids
                    and id(content.additional_properties) not in anchors.content_metadata_ids
                ):
                    stamp_trajectory_timing(content.additional_properties, timing, overwrite=True)


def intermediate_text_signal(response: ChatResponse[Any]) -> str | None:
    """What the intermediate-text callback receives for *response*, if anything.

    The text the model wrote beside its tool calls; an empty string when the
    response calls tools without such text, which still marks a batch
    boundary so the engine's batch counter stays aligned with the messages;
    None when the response calls no tool.
    """
    text = "".join(content.text or "" for content in intermediate_text_contents(response.messages))
    if text:
        return text
    calls_tools = any(
        content.type == "function_call" and not content.informational_only
        for message in response.messages
        for content in message.contents
    )
    return "" if calls_tools else None


_OPAQUE_ID_LIMIT = 256


def _bounded_opaque_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:_OPAQUE_ID_LIMIT] or None


def resolve_exchange_trace(forwarded: object, *, internal_side_call: bool) -> ExchangeTrace | None:
    """Pick the exchange trace this wire request reports to.

    The kernel loop hands the conversation's trace down in ``client_kwargs``;
    a side call inherits those kwargs verbatim, so it must never report to
    the parent's exchange — it gets its own only when the caller rebound the
    ambient context to a side-call actor (which also covers side calls that
    bypass the loop entirely and arrive with no forwarded trace).
    """
    if not internal_side_call and isinstance(forwarded, ExchangeTrace):
        return forwarded
    context = current_trajectory()
    if context is None or context.actor.kind != ActorKind.SIDE_CALL:
        return None
    # A side call (the approval judge, a title generator, the last-words
    # completer) reports under its own actor and its own exchange.
    return ExchangeTrace(context.with_exchange(new_analytics_id()))


def exchange_request_facts(options: Mapping[str, Any], *, stream: bool) -> dict[str, Any]:
    """``model.exchange.started`` facts the wire client knows at request time.

    The client's own provider name stays out of this: it is the OTel dialect
    label (every OpenAI-compatible client answers ``openai``), while the
    ``provider`` both exchange markers carry is the model profile's — and the
    request facts are spread last, so reporting it here would overwrite the
    profile's answer on the start marker alone.
    """
    facts: dict[str, Any] = {
        "stream": stream,
        "continuation_mode": ContinuationMode.POLL
        if options.get("continuation_token") is not None
        else ContinuationMode.NONE,
    }
    # Bounded like the response's own model identifier: a per-request override
    # comes from a profile's unrestricted chat options, and one long enough to
    # push the line past the writer's budget would turn this start marker into
    # a gap while the terminal — which carries the profile's model — still
    # landed, leaving a close with nothing it closes.
    request_model = _bounded_opaque_id(options.get("model"))
    if request_model is not None:
        facts["request_model"] = request_model
    return facts


def exchange_response_facts(response: ChatResponse[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``model.exchange.finished`` facts and measurements taken from a landed response."""
    payload: dict[str, Any] = {
        key: response.additional_properties[key]
        for key in ("request_attempt_id", "provider_request_id")
        if key in response.additional_properties
    }
    response_id = _bounded_opaque_id(response.response_id)
    if response_id is not None:
        payload["response_id"] = response_id
    if response.model:
        payload["response_model"] = str(response.model)[:_OPAQUE_ID_LIMIT]
    if response.finish_reason is not None:
        payload["finish_reason"] = str(response.finish_reason)
    usage = response.usage_details
    reported = provider_reported_usage(usage)
    normalized = normalized_usage(usage)
    usage_payload: dict[str, Any] = {"normalized": normalized}
    if reported is not None:
        usage_payload["provider_reported"] = reported.values
        if reported.omitted:
            # Naming what did not fit keeps a reader from reading the mirror
            # as everything the provider said.
            usage_payload["provider_reported_omitted"] = list(reported.omitted)
    payload["usage"] = usage_payload
    return payload, usage_measurements(normalized)


def _update_has_visible_text(update: Any) -> bool:
    contents = update.contents
    if not contents:
        return False
    return any(content.type == "text" and content.text for content in contents)


class ExchangeRecorder:
    """Drive an :class:`ExchangeTrace` from one wire request's result.

    *owned* says whether this client minted the trace. A trace the loop
    handed down belongs to the loop: this recorder reports what it can see
    (the landed response) and leaves every abnormal ending to the layer that
    knows which one it was.
    """

    __slots__ = ("_owned", "_trace")

    def __init__(self, trace: ExchangeTrace, *, owned: bool) -> None:
        self._trace = trace
        self._owned = owned

    @property
    def context(self) -> TrajectoryContext:
        return self._trace.context

    def started(self, options: Mapping[str, Any], *, stream: bool) -> None:
        self._trace.started(payload=exchange_request_facts(options, stream=stream))

    def finished(self, response: ChatResponse[Any]) -> None:
        payload, measurements = exchange_response_facts(response)
        self._trace.finished(outcome=ExchangeOutcome.SUCCESS, payload=payload, measurements=measurements)

    def failed(self, exc: BaseException) -> None:
        if not self._owned:
            # The loop closes its own exchanges with the outcome it knows
            # (stalled, interrupted, retryable) while unwinding, and the
            # trace's first-close-wins guard would let this coarser verdict
            # beat it there.
            return
        outcome = ExchangeOutcome.CANCELLED if isinstance(exc, asyncio.CancelledError) else ExchangeOutcome.ERROR
        payload: dict[str, Any] = {}
        if outcome == ExchangeOutcome.ERROR:
            payload["error_code"] = type(exc).__name__
        self._trace.finished(outcome=outcome, payload=payload)

    def attach_stream(self, stream: ResponseStream[Any, ChatResponse[Any]]) -> None:
        trace = self._trace

        def _observe(update: Any) -> Any:
            trace.chunk_observed(visible=_update_has_visible_text(update))
            return update

        stream.with_transform_hook(_observe)
        stream.with_result_hook(self._finish_stream)
        if self._owned:
            # A trace this client opened for itself (a side call below the
            # kernel) has nobody above it to close the exchange, so a stream
            # that dies before its final response has to report its own end.
            stream.with_cleanup_hook(lambda: self._close_dropped_stream(stream))

    def _finish_stream(self, response: ChatResponse[Any]) -> ChatResponse[Any]:
        self.finished(response)
        return response

    def _close_dropped_stream(self, stream: ResponseStream[Any, ChatResponse[Any]]) -> None:
        """Close an exchange whose stream never reached a final response.

        Runs as a cleanup hook, which also fires on the way to a successful
        finalization — where the stream reports neither an error nor an
        abandonment and this does nothing.
        """
        error = _stream_error_of(stream)
        if error is not None:
            self.failed(error)
        elif _stream_abandoned(stream):
            self._trace.abandon()

    def wrap_awaitable(self, awaitable: Awaitable[ChatResponse[Any]]) -> Awaitable[ChatResponse[Any]]:
        async def _observe() -> ChatResponse[Any]:
            try:
                response = await awaitable
            except BaseException as exc:
                self.failed(exc)
                raise
            self.finished(response)
            return response

        return _observe()


def open_exchange(
    messages: Sequence[Message],
    options: Mapping[str, Any],
    *,
    stream: bool,
    forwarded_trace: object,
    internal_side_call: bool,
) -> ExchangeRecorder | None:
    """Resolve the exchange this request reports to and write its start marker.

    Returns None when no trace is active for the request.
    """
    trace = resolve_exchange_trace(forwarded_trace, internal_side_call=internal_side_call)
    if trace is None:
        return None
    if not internal_side_call:
        # The exact request this acquisition sends, as a revision of the
        # actor's context chain. Named before the start marker is written,
        # because both markers carry it and only the start one survives a
        # process that dies mid-request.
        trace.set_context_revision(record_context_revision(trace.context, messages))
    # A trace the resolver minted here belongs to this client; one the loop
    # handed down is closed by the loop.
    recorder = ExchangeRecorder(trace, owned=trace is not forwarded_trace)
    recorder.started(options, stream=stream)
    return recorder


class WireCallObserver:
    """Report every request one wire client sends; see the module docstring.

    The callbacks are fixed for the client's lifetime, while each acquisition
    gets its own :class:`WireCall`, so concurrent requests through one client
    share no state.
    """

    __slots__ = ("_on_intermediate_text_async", "_on_intermediate_text_sync")

    def __init__(
        self,
        *,
        on_intermediate_text_async: Callable[[str], Awaitable[None]] | None = None,
        on_intermediate_text_sync: Callable[[str], None] | None = None,
    ) -> None:
        self._on_intermediate_text_async = on_intermediate_text_async
        self._on_intermediate_text_sync = on_intermediate_text_sync

    def begin(
        self,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        *,
        stream: bool,
        forwarded_trace: object,
    ) -> WireCall:
        """Open one acquisition, before the request is built."""
        internal_side_call = in_internal_side_call()
        recorder = open_exchange(
            messages,
            options,
            stream=stream,
            forwarded_trace=forwarded_trace,
            internal_side_call=internal_side_call,
        )
        if internal_side_call:
            return WireCall(recorder, stream=stream)
        return WireCall(
            recorder,
            stream=stream,
            timing=_ResponseTiming.start(messages),
            on_intermediate_text_async=self._on_intermediate_text_async,
            on_intermediate_text_sync=self._on_intermediate_text_sync,
        )


class WireCall:
    """One acquisition's reporting; *timing* is None for an internal side call."""

    __slots__ = ("_on_async", "_on_sync", "_recorder", "_requests", "_stream", "_timing")

    def __init__(
        self,
        recorder: ExchangeRecorder | None,
        *,
        stream: bool,
        timing: _ResponseTiming | None = None,
        on_intermediate_text_async: Callable[[str], Awaitable[None]] | None = None,
        on_intermediate_text_sync: Callable[[str], None] | None = None,
    ) -> None:
        self._recorder = recorder
        self._requests = RequestTracking(recorder.context if recorder is not None else None)
        self._stream = stream
        self._timing = timing
        self._on_async = on_intermediate_text_async
        self._on_sync = on_intermediate_text_sync

    def failed(self, exc: BaseException) -> None:
        """The request failed before it returned a response or a stream."""
        if self._recorder is not None:
            self._recorder.failed(exc)

    def observe(self, result: Any) -> Any:
        """Hook this acquisition's reporting into the request's result.

        A stream is returned as is, with hooks added and nothing consumed:
        higher layers rely on its lazy resolution. A pending response is
        wrapped in a coroutine that reports it once it lands.
        """
        if self._stream:
            if isinstance(result, ResponseStream):
                self._observe_stream(result)
            return result
        observed = self._track_request(result)
        observed = self._recorder.wrap_awaitable(observed) if self._recorder is not None else observed
        if self._timing is None:
            return observed
        return self._land(observed, self._timing)

    def _observe_stream(self, stream: ResponseStream[ChatResponseUpdate, ChatResponse[Any]]) -> None:
        stream.with_pull_context_manager(self._requests.scope)
        stream.with_result_hook(self._stamp_request)
        if self._recorder is not None:
            self._recorder.attach_stream(stream)
        timing = self._timing
        if timing is None:
            return
        on_sync = self._on_sync

        def _on_finalized(response: ChatResponse[Any]) -> ChatResponse[Any]:
            timing.stamp(response)
            if on_sync is not None and (signal := intermediate_text_signal(response)) is not None:
                on_sync(signal)
            return response

        stream.with_result_hook(_on_finalized)

    async def _land(self, observed: Awaitable[ChatResponse[Any]], timing: _ResponseTiming) -> ChatResponse[Any]:
        response = await observed
        timing.stamp(response)
        if self._on_async is not None and (signal := intermediate_text_signal(response)) is not None:
            await self._on_async(signal)
        return response

    async def _track_request(self, result: Awaitable[ChatResponse[Any]]) -> ChatResponse[Any]:
        with self._requests.scope():
            response = await result
        return self._stamp_request(response)

    def _stamp_request(self, response: ChatResponse[Any]) -> ChatResponse[Any]:
        response.additional_properties.update(self._requests.response_facts())
        attempt_id = self._requests.request_attempt_id
        if attempt_id is not None and self._timing is not None:
            # Messages only, like the exchange's operation id: a tool result
            # inherits its call content's properties, and some encoders send
            # a content's properties on the wire.
            anchors = self._timing.echo_anchors
            for message in response.messages:
                if id(message.additional_properties) not in anchors.message_metadata_ids:
                    message.additional_properties[REQUEST_ATTEMPT_ID_METADATA] = attempt_id
        return response
