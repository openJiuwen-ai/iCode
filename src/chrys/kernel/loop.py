# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Model/tool loop over a chat middleware layer and a provider wire client.

``ToolLoopLayer`` owns per-run middleware pipelines, dispatch, result assembly,
continuation state and interrupt-recovery recording. Each ``get_response``
builds one ``_LoopRun`` (the blocking and streaming drivers) over a
``_WireCaller`` (logical model calls and their wire retry lanes) and a
``_LoopTrajectory`` (cycle, exchange and retry events, and the landed tool
operations not yet dispatched). Tool calls execute in ``_tool_execution``;
the interrupt-recovery journal is ``_loop_recorder``.

Tool containers become fresh run-local lists, since direct callers and
progressive exposure can add tools after agent preparation. Inner stream
result hooks run once; final assembly must not replay them.

Kernel dependencies stay within this package, allowed third-party packages
and foundation. Intra-package imports are relative.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Mapping, Sequence
from copy import copy
from dataclasses import dataclass
from enum import Enum
from time import monotonic_ns
from typing import TYPE_CHECKING, Any, Protocol, cast

from chrys.foundation.errors import (
    ProviderResponseError,
    classify_error,
    clean_error_message,
    invalidates_continuation_token,
    is_blank_response,
    is_context_overflow,
    is_thinking_binding_rejection,
)
from chrys.foundation.retry import StreamStall
from chrys.foundation.tool_invocation_order import TOOL_INVOCATION_ORDER_KEY
from chrys.foundation.trajectory.context import (
    TRAJECTORY_CONTEXT_KWARG,
    TRAJECTORY_EXCHANGE_KWARG,
    ExchangeTrace,
    TrajectoryContext,
    current_trajectory,
    trajectory_scope,
)
from chrys.foundation.trajectory.envelope import MeasurementSource, measurement
from chrys.foundation.trajectory.event_types import (
    EventType as TrajectoryEventType,
)
from chrys.foundation.trajectory.event_types import (
    ExchangeOutcome,
    RetryMode,
    RetryReason,
    ToolOutcome,
)
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.metadata import (
    ANALYTICS_ITEM_ID_KEY,
    OPERATION_ID_KEY,
)
from chrys.foundation.trajectory.usage import exchange_usage_payload

from ._content import Content, add_usage_details, normalize_stream_usage
from ._loop_recorder import LoopRecorder, _message_snapshot
from ._tool_execution import (
    _execute_function_calls,
    _is_actionable_function_call,
    _record_unexecuted_tool_operation,
)
from ._types import (
    ChatResponse,
    ChatResponseUpdate,
    Message,
    ResponseStream,
)
from .client import (
    EXHAUSTION_TAIL_KWARG,
    _wire_message_view,
    resolve_storage_mode_and_handles,
    start_with_wire_progress,
)
from .compaction import ContextOverflowSink
from .exchanges import TOOL_CALL_CONTENT_TYPES
from .identity import WeakIdentityRegistry
from .middleware import FunctionMiddlewarePipeline, _as_middleware_list, split_middleware
from .sessions import AgentSession, is_local_history_conversation_id
from .tools import (
    normalize_tools,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Awaitable, Callable

    from chrys.foundation.trajectory.envelope import EventDraft

    from ._content import UsageDetails
    from .middleware import ChatMiddleware, ChatMiddlewareLayer, FunctionMiddleware

logger = logging.getLogger(__name__)

# Default bounds on model/tool iterations and consecutive failed tool batches.
DEFAULT_MAX_ITERATIONS = 40
DEFAULT_MAX_CONSECUTIVE_ERRORS = 3

# Pause between continuation polls of a still-running background response.
# Retrieval returns immediately while the response is queued/in_progress, so
# an unpaced loop would hammer the provider straight into rate limits.
CONTINUATION_POLL_INTERVAL_SECONDS: float = 2.0


RESPONSE_READY_KWARG = "response_ready_observer"
"""``client_kwargs`` key of the run's :data:`ResponseReadyObserver`."""

type ResponseReadyObserver = Callable[[ChatResponse], Awaitable[None]]
"""Awaited once per landed model response, before the loop runs its tools or returns it.

Both drivers call it for every response, the final request without tools
included, after the response is normalized (echoes removed, unexecutable
calls stripped, fallback text added). A streamed response's updates were
all yielded before it. An exception it raises fails the run before the
response's tools run.
"""


class StallExhaustedAction(Enum):
    """Action taken when the stream-idle retry budget is exhausted."""

    BLOCKING_FALLBACK = "blocking_fallback"
    RAISE = "raise"


class WireRetryPolicy(Protocol):
    """Per-run policy for retrying one logical provider call."""

    max_retries: int
    stall_timeout_seconds: float | None
    stall_max_retries: int
    stall_exhausted_action: StallExhaustedAction
    # Probe for the hosted tool calls a replay must not re-run (side-effectful
    # or of unknown safety) that the current wire attempt already ran
    # server-side; None when the caller has no such probe.
    hosted_commits_in_flight: Callable[[], tuple[str, ...]] | None
    # Whether the answer text the current call streamed was shown, for a
    # caller that shows it only once a whole segment of it completes; None
    # when every answer text the call yields shows as it arrives.
    answer_shown: Callable[[], bool] | None

    def backoff_seconds(self, attempt: int) -> int: ...

    def is_retryable(self, exc: BaseException) -> bool: ...

    def is_interrupted(self) -> bool: ...

    async def sleep(self, seconds: int) -> bool: ...

    async def on_retry(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        exc: BaseException,
    ) -> None: ...

    def before_retry(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ConsumedInjectionMessageProbe:
    """Downward-safe seam for clean injections consumed on a wire call.

    Chat middleware may append transient reminders after injection.  The
    service-owned probe exposes only the clean user messages so a later tool
    iteration can retain the user's instruction without persisting wire-only
    enrichment.
    """

    drain_consumed_injection_messages: Callable[[], list[Message]]
    commit_consumed_injections: Callable[[], None] | None = None

    def take_consumed_messages(self) -> list[Message]:
        """Take clean user messages consumed by the latest wire call."""
        return self.drain_consumed_injection_messages()

    def commit(self) -> None:
        """Commit the latest logical call's injection transaction."""
        if self.commit_consumed_injections is not None:
            self.commit_consumed_injections()


_MAX_ITERATIONS_FALLBACK_TEXT = "Maximum iterations reached before a final answer could be produced."
_CONSECUTIVE_ERRORS_FALLBACK_TEXT = (
    "Tool calls kept failing, so they were stopped before a final answer could be produced."
)
_MAX_FUNCTION_CALLS_FALLBACK_TEXT = "Maximum function calls reached before a final answer could be produced."

_USER_VISIBLE_CONTENT_TYPES = frozenset({"data", "uri", "error", "hosted_file", "hosted_vector_store"})


def _strip_unexecutable_calls_from_response(response: ChatResponse) -> bool:
    """Drop actionable function calls from an exhaustion-tail final response.

    The tail's request carries ``tool_choice="none"``; a provider that ignores
    it returns calls the loop will never execute, and a dangling call recorded
    on this success path has no later repair. Messages the strip empties are
    dropped, and a ``"tool_calls"`` finish reason is normalized to ``"stop"``
    — the stripped response holds no remaining tool work, so advertising it
    would route a plain-text final as unfinished. Returns whether anything
    was removed.
    """
    removed = False
    kept_messages: list[Message] = []
    for message in response.messages:
        if any(_is_actionable_function_call(content) for content in message.contents):
            removed = True
            remaining = [content for content in message.contents if not _is_actionable_function_call(content)]
            if not remaining:
                continue
            message.contents = remaining
        kept_messages.append(message)
    if removed:
        response.messages = kept_messages
        if response.finish_reason == "tool_calls":
            response.finish_reason = "stop"
    return removed


def _strip_unexecutable_calls_from_update(
    update: ChatResponseUpdate, *, suppress_finish_reason: bool = False
) -> ChatResponseUpdate | None:
    """Copy-on-write strip of actionable calls from an exhaustion-tail update.

    Runs in the tail's own yield loop — above the client filter seam, so the
    ResponseStream filter contract ("must return an update") is not involved
    and a fully emptied update can be dropped by returning ``None``. The
    incoming update is never mutated: the retry stream's recorded updates
    must keep the provider's original contents so the post-assembly response
    strip still observes what came back (that observation drives the
    service-storage desync guard). An emptied update still passes through
    when it carries meaningful metadata — any semantic field ``from_updates``
    or a streaming consumer reads (continuation handles, model/timestamps/
    ids, provider metadata); usage survives as a retained content.
    ``raw_representation`` deliberately does NOT exempt: every wire-parsed
    update carries its SDK event, so exempting it would make the drop
    unreachable.

    ``finish_reason`` never exempts and is cleared from every stripped copy:
    the provider concluded the turn on calls this strip is withholding, and
    a consumer keying on the first terminal reason would treat the run as
    complete before the tail's corrective final update re-emits the real
    one. ``suppress_finish_reason`` extends that verdict to updates with no
    call of their own — providers commonly emit the call delta and the
    finish-reason chunk separately, so once the tail has stripped a call the
    caller must suppress every later provider reason too, or the reason-only
    chunk crosses untouched ahead of the corrective update.
    """
    has_actionable_call = any(_is_actionable_function_call(content) for content in update.contents)
    if not has_actionable_call and not (suppress_finish_reason and update.finish_reason is not None):
        return update
    stripped = copy(update)
    stripped.contents = [content for content in update.contents if not _is_actionable_function_call(content)]
    stripped.finish_reason = None
    if (
        stripped.contents
        or any(
            value is not None
            for value in (
                stripped.response_id,
                stripped.conversation_id,
                stripped.continuation_token,
                stripped.model,
                stripped.created_at,
                stripped.message_id,
                stripped.author_name,
            )
        )
        or stripped.additional_properties
    ):
        return stripped
    return None


def _response_has_visible_content(response: ChatResponse) -> bool:
    for message in response.messages:
        for content in message.contents:
            if content.type == "text":
                if content.text and content.text.strip():
                    return True
            elif content.type in _USER_VISIBLE_CONTENT_TYPES:
                return True
    return False


def _ensure_exhaustion_fallback_response(response: ChatResponse, fallback_text: str) -> Content | None:
    """Synthesize *fallback_text* when the final response has nothing visible; return the content added.

    Runs unconditionally after the tail strip (and, in streaming, after echo
    shell cleanup): a blank or reasoning-only final response, which response
    validation lets through on the tail call for this text to answer, would
    otherwise return as an empty success, whether or not the strip removed
    anything.
    """
    if _response_has_visible_content(response):
        return None
    fallback_content = Content.from_text(fallback_text)
    if response.messages and not response.messages[-1].contents:
        response.messages[-1].role = "assistant"
        response.messages[-1].contents = [fallback_content]
    else:
        response.messages.append(Message(role="assistant", contents=[fallback_content]))
    return fallback_content


def _invalidate_service_continuation_state(
    response: ChatResponse,
    session: AgentSession | None,
) -> None:
    """Withhold a stripped, service-stored response's continuation handles.

    The service retains the unstripped transcript (calls with no outputs)
    behind them; propagating a handle would reproduce next turn the 400 the
    strip prevents. Clearing forces authoritative local-history replay. The
    withheld handles — the response's own id included, since a stateful
    provider accepts it back as ``previous_response_id`` — are recorded on
    the session so a caller repeating one under any handle spelling gets
    suppressed instead of re-sent alongside that replay.
    """
    if session is not None:
        for handle in (response.conversation_id, response.response_id, session.service_session_id):
            if isinstance(handle, str) and handle and not is_local_history_conversation_id(handle):
                session.invalidated_service_session_ids.add(handle)
    response.conversation_id = None
    response.response_id = None
    response._chrys_service_state_invalidated = True
    if session is not None and session.service_session_id is not None:
        session.service_session_id = None


# --------------------------------------------------------------------------- #
# Loop helpers (response shaping)
# --------------------------------------------------------------------------- #


def _extract_function_calls(response: ChatResponse) -> list[Content]:
    """Collect unresolved, deduplicated function calls from a response.

    Skips calls that already carry a result
    in the same response and duplicate ``call_id``\\ s. Echoed calls never
    reach this filter — ``_land_response_contents`` removes them from
    the response before anything else observes it.
    """
    function_results = {
        item.call_id
        for message in response.messages
        for item in message.contents
        if item.type == "function_result" and item.call_id
    }
    seen_call_ids: set[str] = set()
    function_calls: list[Content] = []
    for message in response.messages:
        for item in message.contents:
            if not _is_actionable_function_call(item):
                continue
            if item.call_id and item.call_id in function_results:
                continue
            if item.call_id and item.call_id in seen_call_ids:
                continue
            if item.call_id:
                seen_call_ids.add(item.call_id)
            function_calls.append(item)
    return function_calls


_HOSTED_CALL_CONTENT_TYPES = frozenset(TOOL_CALL_CONTENT_TYPES - {"function_call"})
"""Provider-hosted tool invocations a landed response may carry (observed, never executed here)."""


def _hosted_call_kinds(response: ChatResponse) -> list[str]:
    """Content types of the hosted calls in *response*, in emission order."""
    return [
        item.type
        for message in response.messages
        for item in message.contents
        if item.type in _HOSTED_CALL_CONTENT_TYPES
    ]


def _land_response_contents(
    response: ChatResponse,
    next_ordinal: int,
    echo_registry: WeakIdentityRegistry,
    *,
    exchange_operation_id: str | None = None,
) -> int:
    """Normalize a landed response's contents and stamp fresh function calls.

    This loop is the single authoritative producer of ``_chrys_tool_invocation_order``.
    Landing happens at response arrival — before recording, dispatch
    extraction, tool lookup, and argument validation — over every
    non-informational ``function_call`` content in model emission order.
    Calls the dispatch filter drops (already-answered, duplicate ids,
    no-tools batches) and calls that fail pre-pipeline validation still
    consume ordinals, so persisted numbering can never diverge from pipeline
    numbering. Tool-operation ids are assigned later, once response-scoped
    pairing has distinguished work this loop owns from calls the provider
    already answered. Returns the next unassigned ordinal.

    Sole-authority consequences: a pre-existing stamp on a content this run
    has NOT numbered is foreign data (a bridged or cached response replaying
    another run's history) and is overwritten — preserving it while the
    counter advances would let a stale value collide with a fresh assignment.

    ``echo_registry`` holds the identity of every content the conversation
    already holds — the loop seeds it from caller history and response
    contents land here, batch results register in
    ``_handle_function_call_results``; usage stays out via the registry's
    ``ignore_usage`` construction policy. A content OBJECT reappearing
    later (a client echoing prior messages back, or the same object
    repeated within one response) is an echo of history, not new work, and
    is removed before the recorder, the dispatch filter, and final
    transcript assembly observe it: an echoed call would otherwise
    re-execute or persist as a dangling duplicate, and an echoed result
    would duplicate an answer the transcript already holds. Removal is by
    object identity only — a DIFFERENT content reusing an echoed call id
    is fresh work (providers mint per-response counter ids) and lands
    normally; a dead member's recycled address can never match, because
    membership requires the registered object itself to still be alive.
    Identity removal presumes assembly cannot launder an echoed object into
    a new one: ``ChatResponseUpdate`` assembly MERGES consecutive same-call
    function-call fragments (and coalesces adjacent text) into NEW objects,
    so the streaming loop registers ``_strip_echoed_update`` as a per-turn
    update filter on the wire stream — conversation-held objects are
    removed at the innermost stream BEFORE accumulation, so a merge copy of
    an echo never exists, a fresh call reusing an echoed id assembles alone
    with its own arguments, and the provider finalizer plus every result
    hook observe the same echo-free sequence (no re-assembly ever
    overwrites a hook's rewrite of the response). Call-id-level provenance
    cannot do this job: an id carrying both a laundered echo merge and a
    fresh call is ambiguous per id, and either verdict misfires
    (re-executing the echo or swallowing the fresh call via duplicate-id
    dispatch). The reverse direction of the same laundering is covered by
    ``_record_stream_fragment_identities`` after landing: merges mint NEW
    assembled objects, so the raw fragments a client just streamed would
    otherwise stay unknown to the memo and could be replayed next turn.

    Message wrappers are never shared with the client, in either direction.
    Every retained message is REPLACED in ``response.messages`` by a
    loop-owned shallow copy (own contents list, copied
    ``additional_properties``) — or dropped when nothing fresh remains.
    That isolation cuts both ways: echo removal never mutates the incoming
    message (the same object may sit in the accumulated transcript, and a
    client-held alias must keep its view), and a stateful client that later
    mutates a message object it returned — appending a fresh call between
    iterations — cannot rewrite the history this run already landed.
    The request direction is covered too: ``_WireCaller._call`` sends EVERY
    outgoing message — caller history included — as a per-call view
    (``_wire_message_view``), so mutating an INPUT message can corrupt
    neither landed history nor the caller's session-state objects; message
    metadata still writes through because views share the wrapper's
    ``additional_properties`` dict. ``messages`` is rebound, not mutated,
    so caller-held aliases of the original list keep their view. The
    CONTENT objects stay the originals: the registry references them
    weakly, so an entry lives exactly as long as its object and a
    collected content simply stops being a member.

    Landing is also where the trajectory identities are minted, under the
    same sole-authority rule as the ordinal: every retained message gets a
    fresh analytics item id and is bound to ``exchange_operation_id`` (the
    wire exchange that produced it); every fresh function call gets its own
    item id. A pre-existing operation stamp is foreign data (a replayed or
    cached response) and is removed, never trusted. The dispatch collector
    later assigns one operation id per unresolved call id; duplicate call
    contents share it, while a provider-answered call has no Chrys tool
    operation to name.
    """
    normalized_messages: list[Message] = []
    for message in response.messages:
        fresh_contents: list[Content] = []
        dropped_echo = False
        for item in message.contents:
            if item in echo_registry:
                dropped_echo = True
                continue
            echo_registry.register(item)
            if _is_actionable_function_call(item):
                item.additional_properties[TOOL_INVOCATION_ORDER_KEY] = next_ordinal
                next_ordinal += 1
                item.additional_properties.pop(OPERATION_ID_KEY, None)
            if item.type == "function_call":
                item.additional_properties[ANALYTICS_ITEM_ID_KEY] = new_analytics_id()
            fresh_contents.append(item)
        if dropped_echo and not fresh_contents:
            continue
        # Snapshot the wrapper, keep the contents: the client holds its own
        # reference to this message and may mutate it between iterations.
        snapshot = _message_snapshot(message, fresh_contents)
        snapshot.additional_properties[ANALYTICS_ITEM_ID_KEY] = new_analytics_id()
        if exchange_operation_id is not None:
            snapshot.additional_properties[OPERATION_ID_KEY] = exchange_operation_id
        normalized_messages.append(snapshot)
    response.messages = normalized_messages
    return next_ordinal


def _stamp_function_call_operations(response: ChatResponse, function_calls: Sequence[Content]) -> None:
    """Assign one tool operation to each unresolved call the loop may dispatch.

    ``_extract_function_calls`` is the response-scoped pairing authority: a
    call already answered in the same response is provider-owned history, not
    a Chrys tool operation. Repeated unresolved ``call_id`` contents describe
    the same dispatch (the extractor keeps the first), so they share that
    first call's operation id rather than opening operations nobody executes.
    Empty ids are not deduplicated and therefore keep one operation per call.
    """
    operation_by_identity: dict[int, str] = {}
    operation_by_call_id: dict[str, str] = {}
    for function_call in function_calls:
        operation_id = new_analytics_id()
        function_call.additional_properties[OPERATION_ID_KEY] = operation_id
        operation_by_identity[id(function_call)] = operation_id
        if function_call.call_id:
            operation_by_call_id[function_call.call_id] = operation_id

    for message in response.messages:
        for item in message.contents:
            if not _is_actionable_function_call(item) or id(item) in operation_by_identity:
                continue
            operation_id = operation_by_call_id.get(item.call_id or "")
            if operation_id is not None:
                item.additional_properties[OPERATION_ID_KEY] = operation_id


def _strip_echoed_update(
    update: ChatResponseUpdate,
    echo_registry: WeakIdentityRegistry,
) -> ChatResponseUpdate:
    """The update with conversation-held content objects removed.

    Registered per turn as an update filter on the wire stream
    (:meth:`ResponseStream.with_update_filter`), so it runs at the innermost
    stream BEFORE accumulation: update assembly merges consecutive same-call
    function-call fragments (and coalesces adjacent text) into NEW objects,
    so an echoed historical object that participates in a merge would
    launder its identity past the echo memo. Filtering ahead of assembly
    means a laundered merge copy never exists, a fresh call reusing an
    echoed call id assembles alone with its own arguments, and — because
    the provider finalizer and every result hook see the already-filtered
    updates — no re-assembly ever overwrites a hook's rewrite of the
    response ("hooks run before tool extraction" stays true). The incoming
    update is never mutated: one carrying an echo is shallow-copied with a
    filtered contents list, so a producer-held alias keeps its shape.
    The copy carries private, non-serialized provenance through stream
    proxies and assembly, letting the loop drop only the logical message
    shell this update actually emptied. The marker is attached to the
    accepted update object rather than keyed by provider message ID, which
    can collide across resent requests.
    """
    if any(item in echo_registry for item in update.contents):
        update = copy(update)
        update._chrys_echo_content_stripped = True
        update.contents = [item for item in update.contents if item not in echo_registry]
    return update


def _remove_echo_emptied_message_shells(response: ChatResponse) -> None:
    """Remove only accepted-stream messages left empty by echo stripping.

    A call-wide marker cannot distinguish an unrelated legitimate empty
    message from an echo-only one. Per-update provenance is folded into the
    assembled Message, so this cleanup remains per message and per accepted
    validation attempt without trusting non-unique provider message IDs.
    """
    response.messages = [
        message for message in response.messages if message.contents or not message._chrys_echo_content_stripped
    ]


def _record_stream_fragment_identities(
    stream: ResponseStream[ChatResponseUpdate, ChatResponse],
    echo_registry: WeakIdentityRegistry,
) -> None:
    """Record the identity of every accepted stream content.

    Landing only sees the ASSEMBLED response, and assembly merges
    multi-fragment calls (and coalesces adjacent text) into NEW objects —
    so the raw fragment objects the client actually yielded would never
    enter the echo registry, and a stateful client replaying a buffered
    fragment on a later turn would sail past ``_strip_echoed_update`` and
    re-execute the call. The consumed stream's accumulated updates are the
    accepted (post-strip) sequence; under the response-validation proxy
    they are the SELECTED attempt's replay, so failed attempts' fragments
    are not recorded and can be legitimately re-sent on a retry.

    Must run AFTER ``_land_response_contents``: a single-fragment call's
    assembled content IS the fragment object, and pre-registering it
    would make landing drop the fresh call as its own echo.

    No retention side: a replay requires the client to still HOLD the
    fragment, which keeps the weak entry alive; a collected fragment's
    entry evicts, so its recycled id can never strip a genuinely fresh
    content. Usage is request-scoped accounting rather than conversation
    content (a client may reuse a mutable usage object across model
    calls), so the registry's ``ignore_usage`` policy keeps it out.
    """
    for update in stream.updates:
        for item in update.contents:
            echo_registry.register(item)


def _record_wire_request_content_identities(
    messages: Sequence[Message],
    echo_registry: WeakIdentityRegistry,
) -> None:
    """Register the post-middleware contents of one actual wire request.

    Called at the chat pipeline's final-handler boundary, after every
    middleware has transformed the request and immediately before the inner
    client sees it. This closes the identity-registry gap for wire-only
    messages such as injections and reminders, including repeated
    ``call_next`` retries.

    No retention side: a weak entry lives exactly as long as its object,
    so a collected wire-only enrichment's entry evicts and its recycled id
    can never strip a genuinely fresh content. Usage is request-scoped
    accounting; the registry's ``ignore_usage`` policy keeps it out.
    """
    for message in messages:
        for item in message.contents:
            echo_registry.register(item)


def _stream_usage_chunks(update: ChatResponseUpdate) -> list[Mapping[str, Any]]:
    """Return usage snapshots carried by one streaming update."""
    return [
        content.usage_details
        for content in update.contents
        if content.type == "usage" and content.usage_details is not None
    ]


def _truncated_final_function_call_ids(response: ChatResponse, function_calls: Sequence[Content]) -> set[int]:
    """Return the object id for the final content block when it is a function call."""
    if response.finish_reason != "length" or not function_calls:
        return set()
    final_content = next(
        (item for message in reversed(response.messages) for item in reversed(message.contents)),
        None,
    )
    if final_content is None or not _is_actionable_function_call(final_content):
        return set()
    function_call_ids = {id(item) for item in function_calls}
    final_id = id(final_content)
    return {final_id} if final_id in function_call_ids else set()


def _prepend_fcc_messages(response: ChatResponse, fcc_messages: list[Message]) -> None:
    """Insert accumulated loop messages before the final response."""
    if not fcc_messages:
        return
    for msg in reversed(fcc_messages):
        response.messages.insert(0, msg)


def _update_conversation_id(
    kwargs: dict[str, Any],
    conversation_id: str | None,
    options: dict[str, Any] | None = None,
) -> None:
    """Write a service conversation id back into kwargs/options."""
    if conversation_id is None:
        return
    if "chat_options" in kwargs:
        kwargs["chat_options"]["conversation_id"] = conversation_id
    else:
        kwargs["conversation_id"] = conversation_id
    if options is not None:
        options["conversation_id"] = conversation_id


def _update_continuation_state(
    kwargs: dict[str, Any],
    response: ChatResponse,
    *,
    session: AgentSession | None,
    options: dict[str, Any] | None = None,
) -> None:
    """Update in-flight and persisted continuation state from a response."""
    conversation_id = response.conversation_id
    if conversation_id is None:
        return

    _update_conversation_id(kwargs, conversation_id, options)
    if (
        session is not None
        and not response.has_internal_conversation_id()
        and session.service_session_id != conversation_id
    ):
        session.service_session_id = conversation_id


def _clear_internal_conversation_id(response: ChatResponse) -> ChatResponse:
    """Strip a client-internal conversation id before returning."""
    if response.has_internal_conversation_id():
        response.conversation_id = None
        response.clear_internal_conversation_id()
    return response


def _handle_function_call_results(
    *,
    response: ChatResponse,
    function_call_results: list[Content],
    fcc_messages: list[Message],
    echo_registry: WeakIdentityRegistry,
    errors_in_a_row: int,
    had_errors: bool,
    max_errors: int,
    result_carrier_item_id: str,
    recorder: LoopRecorder | None = None,
) -> tuple[str, int]:
    """Fold a batch of tool results back into the response.

    Results are ``function_result`` contents. Returns
    ``(action, errors_in_a_row)`` where action is
    ``"continue"`` or ``"stop"`` (consecutive-error cap reached: submit the
    collected results once more with tools disabled).

    New results register in the echo registry —
    they are part of the run's transcript from here on, so a client echoing
    them back in a later response must see them removed as history rather
    than landing a duplicate answer (``_land_response_contents``).
    """
    for result in function_call_results:
        echo_registry.register(result)
    if had_errors:
        errors_in_a_row += 1
        reached_error_limit = errors_in_a_row >= max_errors
        if reached_error_limit:
            logger.warning(
                "Maximum consecutive function call errors reached (%d). "
                "Stopping further function calls for this request.",
                max_errors,
            )
    else:
        errors_in_a_row = 0
        reached_error_limit = False

    result_message = Message(role="tool", contents=function_call_results)
    # The carrier is a persisted item of its own. Its batch-wide id was minted
    # at dispatch, before any tool terminal named it, so the next context
    # revision and every tool.operation.finished agree on this same item.
    result_message.additional_properties[ANALYTICS_ITEM_ID_KEY] = result_carrier_item_id
    response.messages.append(result_message)
    if recorder is not None:
        recorder.seal_exchange(result_message)
    fcc_messages.extend(response.messages)
    return ("stop" if reached_error_limit else "continue", errors_in_a_row)


# --------------------------------------------------------------------------- #
# Run building blocks
# --------------------------------------------------------------------------- #

# The first tracked continuation token is always applied: nothing equals it.
_UNTRACKED = object()


def _wire_request_observer(
    echo_registry: WeakIdentityRegistry,
    caller_observer: Callable[[Sequence[Message]], None] | None,
) -> Callable[[Sequence[Message]], None]:
    internal_observer = functools.partial(
        _record_wire_request_content_identities,
        echo_registry=echo_registry,
    )
    if caller_observer is None:
        return internal_observer

    def observe(messages: Sequence[Message]) -> None:
        # Echo tracking remains the loop's invariant even if a caller
        # observer raises; callers then see the exact same provider
        # views immediately after the internal identity recorder.
        internal_observer(messages)
        caller_observer(messages)

    return observe


async def _watchdog_await(awaitable: Awaitable[Any], timeout: float | None, label: str) -> Any:
    # Idle timing: a pull whose first byte waits on compaction (and
    # its LAST_WORDS side call) stays alive while that work reports
    # progress, and stalls after *timeout* without any.
    if timeout is None:
        return await awaitable
    event_loop = asyncio.get_running_loop()
    last_progress = event_loop.time()

    def _on_progress() -> None:
        nonlocal last_progress
        last_progress = event_loop.time()

    task = start_with_wire_progress(awaitable, _on_progress)
    try:
        while not task.done():
            idle_budget = last_progress + timeout - event_loop.time()
            if idle_budget <= 0:
                break
            await asyncio.wait((task,), timeout=idle_budget)
    except asyncio.CancelledError:
        # The pull task may still be running INSIDE the stream's
        # generator; closing that stream before the task settles
        # would raise "asynchronous generator is already running".
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    if task.done():
        return task.result()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    raise StreamStall(f"{label} produced no progress for {timeout:g}s")


class _LoopTrajectory:
    """Trajectory bookkeeping for one loop run.

    One ``model.cycle`` per loop acquisition, one ``model.exchange`` per wire
    attempt, ``retry.*`` around every wire retry; all of them are no-ops
    without a bound context. The exchange trace rides to the wire client in
    ``client_kwargs`` — the loop cannot bind it ambiently because the provider
    stream is lazy and resolves outside this call's context.

    State, by lifetime:

    - cycle, from ``cycle_started`` to ``cycle_finished`` or ``abort``: the
      cycle context, its start time, exchange count and last exchange id;
    - wire attempt, from ``begin_exchange`` to ``close_exchange``: the open
      exchange trace;
    - retry, from ``retry_scheduled`` to the next ``begin_exchange``: the id
      minted for the next exchange;
    - landing, from one ``cycle_finished`` to the next: the exchange context
      the landed tool operations and hosted calls hang under;
    - landing to dispatch: the tool operations minted for landed calls. This
      ledger runs with or without a bound context; each entry settles under
      the context it landed in.

    ``retry_policy`` is read only to classify a failed exchange as retryable.
    """

    def __init__(self, root: TrajectoryContext | None, *, retry_policy: WireRetryPolicy | None) -> None:
        self._root = root
        self._retry_policy = retry_policy
        self._cycle: TrajectoryContext | None = None
        self._cycle_started_ns = 0
        self._cycle_exchange_count = 0
        self._active_exchange: ExchangeTrace | None = None
        self._pending_exchange_id: str | None = None
        self._last_exchange_id: str | None = None
        self._landed_exchange: TrajectoryContext | None = None
        self._undispatched_tool_operations: dict[int, tuple[Content, TrajectoryContext | None]] = {}

    @property
    def last_exchange_id(self) -> str | None:
        return self._last_exchange_id

    async def _emit(self, draft: EventDraft) -> None:
        if self._root is None:
            return
        try:
            await self._root.sink.emit(draft)
        except Exception:
            logger.debug("Trajectory emit failed", exc_info=True)

    async def cycle_started(self, cycle_index: int, *, tools_offered: bool) -> None:
        root = self._root
        if root is None:
            return
        cycle_id = new_analytics_id()
        cycle = root.with_cycle(cycle_id)
        self._cycle = cycle
        self._cycle_started_ns = monotonic_ns()
        self._cycle_exchange_count = 0
        self._last_exchange_id = None
        await self._emit(
            cycle.draft(
                TrajectoryEventType.MODEL_CYCLE_STARTED,
                operation_id=cycle_id,
                parent_operation_id=root.run_operation_id,
                payload={"cycle_index": cycle_index, "tools_offered": tools_offered},
            )
        )

    def _cycle_finished_draft(self, *, outcome: str, function_call_count: int = 0) -> EventDraft | None:
        cycle = self._cycle
        if cycle is None or cycle.cycle_operation_id is None:
            return None
        self._landed_exchange = cycle.with_exchange(self._last_exchange_id)
        return cycle.draft(
            TrajectoryEventType.MODEL_CYCLE_FINISHED,
            operation_id=cycle.cycle_operation_id,
            parent_operation_id=cycle.run_operation_id,
            payload={
                "outcome": outcome,
                "exchange_count": self._cycle_exchange_count,
                "function_call_count": function_call_count,
                # Monotonic at both ends: a wall clock that steps mid-cycle
                # would otherwise stretch or flatten the span it measures.
                "duration_ms": max(0, (monotonic_ns() - self._cycle_started_ns) // 1_000_000),
                "final_exchange_operation_id": self._last_exchange_id,
            },
            measurements={"/payload/duration_ms": measurement(MeasurementSource.MONOTONIC_CLOCK, method_version=1)},
        )

    async def cycle_finished(self, response: ChatResponse, function_calls: Sequence[Content]) -> None:
        """Close the cycle *response* landed in and hand its calls to the operation ledger."""
        draft = self._cycle_finished_draft(outcome=ExchangeOutcome.SUCCESS, function_call_count=len(function_calls))
        self._register_landed_tool_operations(response, function_calls)
        if draft is None:
            return
        await self._hosted_calls(response)
        # Given up only here: an interrupt during the hosted-call records
        # above still leaves the cycle for the abort path to close.
        self._cycle = None
        await self._emit(draft)

    async def _hosted_calls(self, response: ChatResponse) -> None:
        # Hosted calls never become tool operations (the provider ran
        # them); one fact per call keeps the exchange's fan-out visible
        # without an unbounded array on the exchange event.
        exchange = self._landed_exchange
        if exchange is None or exchange.exchange_operation_id is None:
            return
        for ordinal, hosted_kind in enumerate(_hosted_call_kinds(response)):
            await self._emit(
                exchange.draft(
                    TrajectoryEventType.HOSTED_CALL_OBSERVED,
                    operation_id=new_analytics_id(),
                    parent_operation_id=exchange.exchange_operation_id,
                    payload={
                        "parent_exchange_operation_id": exchange.exchange_operation_id,
                        "hosted_kind": hosted_kind,
                        "ordinal": ordinal,
                    },
                )
            )

    def abort(self, outcome: str) -> None:
        # Synchronous close for exits that cannot await (cancellation,
        # generator close): the open exchange and cycle are queued in
        # order without waiting for the ack.
        self.close_exchange(outcome)
        draft = self._cycle_finished_draft(outcome=outcome)
        self._cycle = None
        if draft is not None and self._root is not None:
            try:
                self._root.sink.emit_soon(draft)
            except Exception:
                logger.debug("Trajectory cycle close failed", exc_info=True)

    def exchange_scope(self) -> trajectory_scope:
        # Ambient context for the tool batch a landed response spawns:
        # tool middleware hangs its operations under the producing
        # exchange. Without a loop-owned context the ambient one (if any)
        # is simply re-bound.
        if self._landed_exchange is None:
            return trajectory_scope(current_trajectory())
        return trajectory_scope(self._landed_exchange)

    def begin_exchange(self, client_kwargs: dict[str, Any]) -> dict[str, Any]:
        """Open the exchange for the next wire attempt; returns the kwargs that carry it."""
        self._active_exchange = None
        cycle = self._cycle
        if cycle is None:
            return client_kwargs
        exchange_id = self._pending_exchange_id or new_analytics_id()
        self._pending_exchange_id = None
        self._last_exchange_id = exchange_id
        self._cycle_exchange_count += 1
        self._active_exchange = ExchangeTrace(cycle.with_exchange(exchange_id))
        return {**client_kwargs, TRAJECTORY_EXCHANGE_KWARG: self._active_exchange}

    def stall_observed(self) -> None:
        if self._active_exchange is not None:
            self._active_exchange.stall_observed()

    def close_exchange(self, outcome: str, *, exc: BaseException | None = None) -> None:
        # Idempotent: a wire client that already reported its own terminal
        # marker wins; this closes the abandoned/failed remainder.
        trace = self._active_exchange
        if trace is None:
            return
        self._active_exchange = None
        self._last_exchange_id = trace.operation_id
        payload: dict[str, Any] = {}
        measurements: dict[str, dict[str, Any]] | None = None
        if exc is not None:
            payload["error_code"] = type(exc).__name__
            policy = self._retry_policy
            payload["retryable"] = bool(policy is not None and policy.is_retryable(exc))
            if not isinstance(exc, asyncio.CancelledError):
                payload["error_kind"] = classify_error(exc).kind
            if isinstance(exc, ProviderResponseError) and exc.usage_details:
                # The failed response still consumed these tokens; the session totals count them too.
                payload["usage"], measurements = exchange_usage_payload(exc.usage_details)
        try:
            trace.finished(outcome=outcome, payload=payload, measurements=measurements)
        except Exception:
            logger.debug("Trajectory exchange close failed", exc_info=True)

    async def retry_scheduled(
        self,
        *,
        reason_code: str,
        retry_mode: str,
        delay_seconds: int,
        fallback_to_blocking: bool,
        committed_work_present: bool,
    ) -> None:
        cycle = self._cycle
        if cycle is None:
            return
        previous = self._last_exchange_id
        next_id = new_analytics_id()
        self._pending_exchange_id = next_id
        await self._emit(
            cycle.draft(
                TrajectoryEventType.RETRY_SCHEDULED,
                operation_id=next_id,
                parent_operation_id=cycle.cycle_operation_id,
                payload={
                    "reason_code": reason_code,
                    "delay_ms": max(0, int(delay_seconds * 1000)),
                    "retry_mode": retry_mode,
                    "previous_operation_id": previous,
                    "committed_work_present": committed_work_present,
                    "fallback_to_blocking": fallback_to_blocking,
                },
            )
        )

    async def retry_started(self, *, retry_mode: str) -> None:
        cycle = self._cycle
        next_id = self._pending_exchange_id
        if cycle is None or next_id is None:
            return
        await self._emit(
            cycle.draft(
                TrajectoryEventType.RETRY_STARTED,
                operation_id=next_id,
                parent_operation_id=cycle.cycle_operation_id,
                payload={
                    "retry_mode": retry_mode,
                    "next_operation_id": next_id,
                    "previous_operation_id": self._last_exchange_id,
                },
            )
        )

    def _register_landed_tool_operations(
        self,
        response: ChatResponse,
        function_calls: Sequence[Content],
    ) -> None:
        """Mint and remember every operation until the one dispatch point takes it."""
        _stamp_function_call_operations(response, function_calls)
        operation_context = self._landed_exchange or current_trajectory()
        for function_call in function_calls:
            self._undispatched_tool_operations[id(function_call)] = (function_call, operation_context)

    def mark_tool_operations_dispatched(self, function_calls: Sequence[Content]) -> None:
        for function_call in function_calls:
            self._undispatched_tool_operations.pop(id(function_call), None)

    async def settle_undispatched_tool_operations(self, *, queued: bool = False) -> None:
        """Close every operation minted but never handed to the execution batch.

        This is the single reconciliation point for no-tools responses,
        exhaustion tails, future early returns, and failures between
        landing and dispatch. Every entry keeps the exchange context it
        landed under rather than consulting the loop's later current one.
        """
        pending = list(self._undispatched_tool_operations.values())
        self._undispatched_tool_operations.clear()
        first_failure: BaseException | None = None
        for function_call, operation_context in pending:
            try:
                with trajectory_scope(operation_context):
                    await _record_unexecuted_tool_operation(
                        function_call,
                        outcome=ToolOutcome.FILTERED,
                        queued=queued,
                    )
            except BaseException as exc:
                # The helper settles the current pair before propagating a
                # cancelled ack. Continue so one cancellation cannot strand
                # the rest of a parallel batch, then preserve the caller's
                # original control-flow signal.
                if first_failure is None:
                    first_failure = exc
        if first_failure is not None:
            raise first_failure


def _carries_answer_text(update: ChatResponseUpdate) -> bool:
    """Whether a streamed update carries answer text a caller may show; reasoning may stream again."""
    return any(content.type == "text" and content.text and not content.text.isspace() for content in update.contents)


class _WireCaller:
    """The loop's logical model calls and their wire retry lanes.

    A logical call is one ``blocking_response`` or ``streaming_response``:
    every request it sends until a response lands — continuation polls,
    transient and stall retries, the stall fallback to a blocking request,
    one context-overflow resend and one resend of a response that showed
    nothing. Retries need a wire policy, which a service-side run never
    gets, and stop once a streaming call's answer text showed: as it was
    yielded, or when the policy's ``answer_shown`` says so.

    State, by lifetime: the continuation token last mirrored to the retry
    owner lives for the run; the two resend flags for one logical call
    (the stall fallback inherits them); the transient and stall counters for
    one ``blocking_response`` call or the streaming attempts of one
    ``streaming_response``, so the stall fallback's blocking request starts
    a fresh transient budget; the provider stream and its usage chunks for
    one request. Of the run's options, only ``continuation_token`` is
    written here.
    """

    def __init__(
        self,
        inner: ChatMiddlewareLayer,
        *,
        options: dict[str, Any],
        client_kwargs: dict[str, Any],
        chat_middleware: list[ChatMiddleware],
        compaction_strategy: Any,
        tokenizer: Any,
        policy: WireRetryPolicy | None,
        recorder: LoopRecorder | None,
        injection_probe: ConsumedInjectionMessageProbe | None,
        continuation_token_observer: Callable[[Any], None] | None,
        trajectory: _LoopTrajectory,
    ) -> None:
        self._inner = inner
        self._options = options
        self._client_kwargs = client_kwargs
        self._chat_middleware = chat_middleware
        self._compaction_strategy = compaction_strategy
        self._tokenizer = tokenizer
        self._policy = policy
        self._recorder = recorder
        self._injection_probe = injection_probe
        self._continuation_token_observer = continuation_token_observer
        self._trajectory = trajectory
        # Sentinel start: the logical call may begin with a retry-owned token
        # already in its options (whole-run retry resuming a background
        # response), so the first tracked value — including a terminal
        # ``None`` — must always be applied, never swallowed by dedupe.
        self._last_tracked_continuation_token: Any = _UNTRACKED

    def cancel_outcome(self) -> str:
        policy = self._policy
        return (
            ExchangeOutcome.INTERRUPTED if policy is not None and policy.is_interrupted() else ExchangeOutcome.CANCELLED
        )

    def _track_continuation_token(self, token: Any) -> None:
        # Mirror the live token into the retry owner's request state: a
        # transient poll failure must resume THIS response on the next
        # attempt (outer whole-run retry included), never re-issue the
        # original create request. Providers re-announce the id on every
        # progress event, so identical repeats are dropped.
        if token == self._last_tracked_continuation_token:
            return
        self._last_tracked_continuation_token = token
        if token is None:
            self._options.pop("continuation_token", None)
        else:
            self._options["continuation_token"] = token
        if self._continuation_token_observer is not None:
            self._continuation_token_observer(token)

    async def _call(
        self,
        prepped: list[Message],
        *,
        as_stream: bool,
        stream_update_filter: Callable[[ChatResponseUpdate], ChatResponseUpdate] | None = None,
        request_message_observer: Callable[[Sequence[Message]], None],
        exhaustion_tail: bool,
    ) -> Any:
        # The recorder keeps the loop's canonical objects (its identity
        # dedup depends on re-recorded history being the SAME objects).
        if self._recorder is not None:
            await self._recorder.record_pre_call(prepped)
        # EVERY outgoing message is a per-call view — caller history
        # included — so a client mutating a received message in place can
        # corrupt neither the loop's transcript nor the caller's
        # session-state objects. Message-metadata write-through survives:
        # views share the wrapper's additional_properties dict, which is
        # how compaction exclusion flags reach stored history.
        wire_view = [_wire_message_view(m) for m in prepped]
        client_kwargs = self._trajectory.begin_exchange(self._client_kwargs)
        if exhaustion_tail:
            # Every request of the tail's logical call carries it: its polls, retries and stall fallback too.
            client_kwargs = {**client_kwargs, EXHAUSTION_TAIL_KWARG: True}
        return self._inner.get_response(
            wire_view,
            stream=as_stream,
            stream_update_filter=stream_update_filter,
            request_message_observer=request_message_observer,
            options=self._options,
            middleware=self._chat_middleware,
            compaction_strategy=self._compaction_strategy,
            tokenizer=self._tokenizer,
            client_kwargs=client_kwargs,
        )

    async def _schedule_retry(
        self,
        policy: WireRetryPolicy,
        exc: BaseException,
        *,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int | None = None,
        fallback_to_blocking: bool = False,
        retry_mode: str | None = None,
    ) -> None:
        if self._options.get("continuation_token") is None:
            # A retry that resumes an already-created response via its
            # continuation token must NOT replay consumed injections: the
            # create that consumed them succeeded (the provider holds
            # them), and a poll never consumes a replay — the batch would
            # sit stranded until commit destroys it.
            policy.before_retry()
        delay = policy.backoff_seconds(attempt - 1) if delay_seconds is None else delay_seconds
        if isinstance(exc, StreamStall):
            reason_code = RetryReason.STREAM_STALL
        elif retry_mode == RetryMode.VALIDATION:
            reason_code = RetryReason.VALIDATION_REJECTED
        else:
            reason_code = RetryReason.TRANSIENT_ERROR
        if retry_mode is None:
            retry_mode = RetryMode.STALL_FALLBACK if fallback_to_blocking else RetryMode.WIRE
        recorder = self._recorder
        await self._trajectory.retry_scheduled(
            reason_code=reason_code,
            retry_mode=retry_mode,
            delay_seconds=delay,
            fallback_to_blocking=fallback_to_blocking,
            committed_work_present=recorder is not None and recorder.committed_count > 0,
        )
        await policy.on_retry(message, attempt, max_attempts, delay, exc)
        if policy.is_interrupted() or await policy.sleep(delay):
            raise asyncio.CancelledError
        await self._trajectory.retry_started(retry_mode=retry_mode)

    async def _pause_between_continuation_polls(self) -> None:
        # Task cancellation is the interrupt channel for both service and
        # local runs; the policy flag check covers soft interrupts that
        # only set state.
        policy = self._policy
        if policy is not None and policy.is_interrupted():
            raise asyncio.CancelledError
        if CONTINUATION_POLL_INTERVAL_SECONDS > 0:
            await asyncio.sleep(CONTINUATION_POLL_INTERVAL_SECONDS)

    def _hosted_commits_vetoing_replay(self, policy: WireRetryPolicy) -> tuple[str, ...]:
        # Without a live continuation token a retry re-creates the request
        # and re-runs the hosted tool calls the failed attempt already
        # executed server-side; with one it merely resumes the same
        # response, which is safe.
        hosted_probe = policy.hosted_commits_in_flight
        hosted_commits = tuple(hosted_probe()) if hosted_probe is not None else ()
        if hosted_commits and self._options.get("continuation_token") is None:
            return hosted_commits
        return ()

    def _note_context_overflow(self, exc: BaseException) -> bool:
        # The provider measured the real input and found the window full:
        # the strategy compacts before the next request instead of letting
        # it resend the rejected input. Runs before any retry decision, so
        # service-side runs (no wire policy) are noted too. Returns whether
        # compacting and resending can help. Thinking the service refused
        # as bound to another conversation is no full window, even when the
        # refusal names it: the client resends without that thinking, or
        # the profile asked for the refusal.
        strategy = self._compaction_strategy
        if (
            not isinstance(strategy, ContextOverflowSink)
            or not is_context_overflow(exc)
            or is_thinking_binding_rejection(exc)
        ):
            return False
        return strategy.note_context_overflow(exc)

    def _resends_after_overflow(self, policy: WireRetryPolicy, *, noted: bool, recovered: bool) -> bool:
        # One in-place resend per logical call, outside the transient and
        # stall budgets. Service-side runs have no wire policy and only
        # keep the note; a live continuation token would poll the rejected
        # response, and hosted work an attempt already ran must not rerun.
        return (
            noted
            and not recovered
            and self._options.get("continuation_token") is None
            and not self._hosted_commits_vetoing_replay(policy)
        )

    def _resends_blank_response(self, policy: WireRetryPolicy, exc: BaseException, *, resent: bool) -> bool:
        # One immediate resend per logical call of a response that showed
        # nothing, outside the transient and stall budgets: sampling again
        # usually answers. Its rejection already dropped the continuation
        # token; hosted work an attempt already ran must not rerun.
        return not resent and is_blank_response(exc) and not self._hosted_commits_vetoing_replay(policy)

    async def _schedule_resend(self, policy: WireRetryPolicy, exc: BaseException, *, retry_mode: str) -> None:
        # After an overflow the strategy holds the note, so the resend's
        # client preparation compacts before the request goes out.
        await self._schedule_retry(
            policy,
            exc,
            message=clean_error_message(exc),
            attempt=1,
            max_attempts=1,
            delay_seconds=0,
            retry_mode=retry_mode,
        )

    async def blocking_response(
        self,
        prepped: list[Message],
        *,
        request_message_observer: Callable[[Sequence[Message]], None],
        overflow_recovered: bool = False,
        blank_resent: bool = False,
        exhaustion_tail: bool = False,
    ) -> ChatResponse:
        retry_attempt = 0
        while True:
            policy = self._policy
            if policy is not None and policy.is_interrupted():
                raise asyncio.CancelledError
            try:
                while True:
                    response = await _resolve_response(
                        await self._call(
                            prepped,
                            as_stream=False,
                            request_message_observer=request_message_observer,
                            exhaustion_tail=exhaustion_tail,
                        )
                    )
                    self._trajectory.close_exchange(ExchangeOutcome.SUCCESS)
                    if response.continuation_token is None:
                        self._track_continuation_token(None)
                        if self._injection_probe is not None:
                            self._injection_probe.commit()
                        return response
                    self._track_continuation_token(response.continuation_token)
                    await self._pause_between_continuation_polls()
            except asyncio.CancelledError:
                self._trajectory.close_exchange(self.cancel_outcome())
                raise
            except Exception as exc:
                self._trajectory.close_exchange(ExchangeOutcome.ERROR, exc=exc)
                if invalidates_continuation_token(exc):
                    # The failure judged a terminal response: a retry must
                    # issue a fresh request, never re-poll the completed
                    # (immutable) one.
                    self._track_continuation_token(None)
                noted = self._note_context_overflow(exc)
                if policy is not None and self._resends_after_overflow(
                    policy, noted=noted, recovered=overflow_recovered
                ):
                    overflow_recovered = True
                    await self._schedule_resend(policy, exc, retry_mode=RetryMode.CONTEXT_OVERFLOW)
                    continue
                if policy is not None and self._resends_blank_response(policy, exc, resent=blank_resent):
                    blank_resent = True
                    await self._schedule_resend(policy, exc, retry_mode=RetryMode.VALIDATION)
                    continue
                if policy is None or not policy.is_retryable(exc) or retry_attempt >= policy.max_retries:
                    raise
                hosted_commits = self._hosted_commits_vetoing_replay(policy)
                if hosted_commits:
                    logger.warning(
                        "Not retrying wire call: provider-hosted tool call(s) already "
                        "executed in the failed attempt (%s)",
                        ", ".join(str(label) for label in hosted_commits),
                    )
                    raise
                retry_attempt += 1
                await self._schedule_retry(
                    policy,
                    exc,
                    message=clean_error_message(exc),
                    attempt=retry_attempt,
                    max_attempts=policy.max_retries,
                )

    def streaming_response(
        self,
        prepped: list[Message],
        *,
        stream_update_filter: Callable[[ChatResponseUpdate], ChatResponseUpdate],
        request_message_observer: Callable[[Sequence[Message]], None],
        exhaustion_tail: bool = False,
    ) -> ResponseStream[ChatResponseUpdate, ChatResponse]:
        final_response: ChatResponse | None = None
        # The provider stream of the attempt being read; None once it was
        # read to its end.
        inner_stream: ResponseStream[ChatResponseUpdate, ChatResponse] | None = None
        logical_stream: ResponseStream[ChatResponseUpdate, ChatResponse]

        async def _updates() -> AsyncIterable[ChatResponseUpdate]:
            nonlocal final_response, inner_stream
            retry_attempt = 0
            stall_retry_attempt = 0
            overflow_recovered = False
            blank_resent = False
            # Whether this call already yielded answer text: callers show it
            # as it arrives (unless the policy says when it shows), so no
            # retry may send the request again.
            delivered_text = False
            while True:
                policy = self._policy
                if policy is not None and policy.is_interrupted():
                    raise asyncio.CancelledError
                inner_stream = None
                try:
                    while True:
                        raw_stream = await self._call(
                            prepped,
                            as_stream=True,
                            stream_update_filter=stream_update_filter,
                            request_message_observer=request_message_observer,
                            exhaustion_tail=exhaustion_tail,
                        )
                        if not isinstance(raw_stream, ResponseStream):
                            raise TypeError("Streaming wire call did not return a ResponseStream.")
                        inner_stream = raw_stream
                        inner_stream.with_update_filter(stream_update_filter)
                        stream_usage_chunks: list[Mapping[str, Any]] = []
                        iterator = inner_stream.__aiter__()
                        while True:
                            try:
                                update = await _watchdog_await(
                                    iterator.__anext__(),
                                    policy.stall_timeout_seconds if policy is not None else None,
                                    "Streaming response",
                                )
                            except StopAsyncIteration:
                                break
                            if update.continuation_token is not None:
                                # Providers announce the background
                                # response id mid-stream; mirror it
                                # immediately so a disconnect before
                                # finalization retries by retrieval, not
                                # by a duplicate create.
                                self._track_continuation_token(update.continuation_token)
                            stream_usage_chunks.extend(_stream_usage_chunks(update))
                            delivered_text = delivered_text or _carries_answer_text(update)
                            yield update
                        response = await _watchdog_await(
                            inner_stream.get_final_response(),
                            policy.stall_timeout_seconds if policy is not None else None,
                            "Streaming response finalization",
                        )
                        inner_stream = None
                        self._trajectory.close_exchange(ExchangeOutcome.SUCCESS)
                        response.latest_usage_details = normalize_stream_usage(stream_usage_chunks)
                        if response.latest_usage_details is None:
                            response.latest_usage_details = response.usage_details
                        if response.continuation_token is None:
                            self._track_continuation_token(None)
                            final_response = response
                            if self._injection_probe is not None:
                                self._injection_probe.commit()
                            return
                        self._track_continuation_token(response.continuation_token)
                        await self._pause_between_continuation_polls()
                except asyncio.CancelledError:
                    self._trajectory.close_exchange(self.cancel_outcome())
                    if inner_stream is not None:
                        await inner_stream.aclose()
                    raise
                except Exception as exc:
                    if isinstance(exc, StreamStall):
                        self._trajectory.stall_observed()
                        self._trajectory.close_exchange(ExchangeOutcome.STALLED, exc=exc)
                    else:
                        self._trajectory.close_exchange(ExchangeOutcome.ERROR, exc=exc)
                    if inner_stream is not None:
                        try:
                            await inner_stream.aclose()
                        except Exception:
                            logger.debug("Failed to close abandoned provider stream", exc_info=True)
                    if invalidates_continuation_token(exc):
                        # The failure judged a terminal response: a retry
                        # must issue a fresh request, never re-poll the
                        # completed (immutable) one.
                        self._track_continuation_token(None)
                    noted = self._note_context_overflow(exc)
                    if policy is None:
                        raise
                    if policy.answer_shown() if policy.answer_shown is not None else delivered_text:
                        # Every lane below (retry, stall retry, blocking
                        # fallback, overflow and blank resends) would show it twice.
                        logger.warning("Not replaying wire call: its streamed text was already delivered")
                        raise
                    hosted_commits = self._hosted_commits_vetoing_replay(policy)
                    if hosted_commits:
                        logger.warning(
                            "Not replaying wire call: provider-hosted tool call(s) already "
                            "executed in the aborted stream (%s)",
                            ", ".join(str(label) for label in hosted_commits),
                        )
                        raise
                    if self._resends_after_overflow(policy, noted=noted, recovered=overflow_recovered):
                        overflow_recovered = True
                        await self._schedule_resend(policy, exc, retry_mode=RetryMode.CONTEXT_OVERFLOW)
                        logical_stream._updates.clear()
                        yield ChatResponseUpdate.retry_boundary()
                        continue
                    if self._resends_blank_response(policy, exc, resent=blank_resent):
                        blank_resent = True
                        await self._schedule_resend(policy, exc, retry_mode=RetryMode.VALIDATION)
                        logical_stream._updates.clear()
                        yield ChatResponseUpdate.retry_boundary()
                        continue
                    if isinstance(exc, StreamStall):
                        if stall_retry_attempt >= policy.stall_max_retries:
                            if policy.stall_exhausted_action is StallExhaustedAction.RAISE:
                                raise
                            await self._schedule_retry(
                                policy,
                                exc,
                                message="Stream stalled; retrying with a blocking response",
                                attempt=stall_retry_attempt + 1,
                                max_attempts=policy.stall_max_retries + 1,
                                delay_seconds=0,
                                fallback_to_blocking=True,
                            )
                            logical_stream._updates.clear()
                            yield ChatResponseUpdate.retry_boundary()
                            final_response = await self.blocking_response(
                                prepped,
                                request_message_observer=request_message_observer,
                                overflow_recovered=overflow_recovered,
                                blank_resent=blank_resent,
                                exhaustion_tail=exhaustion_tail,
                            )
                            return
                        stall_retry_attempt += 1
                        await self._schedule_retry(
                            policy,
                            exc,
                            message="Stream stalled",
                            attempt=stall_retry_attempt,
                            max_attempts=policy.stall_max_retries,
                        )
                        logical_stream._updates.clear()
                        yield ChatResponseUpdate.retry_boundary()
                        continue
                    if not policy.is_retryable(exc) or retry_attempt >= policy.max_retries:
                        raise
                    retry_attempt += 1
                    await self._schedule_retry(
                        policy,
                        exc,
                        message=clean_error_message(exc),
                        attempt=retry_attempt,
                        max_attempts=policy.max_retries,
                    )
                    logical_stream._updates.clear()
                    yield ChatResponseUpdate.retry_boundary()

        def _finalizer(_updates: Sequence[ChatResponseUpdate]) -> ChatResponse:
            if final_response is None:
                raise RuntimeError("Logical streaming call ended without a final response.")
            return final_response

        async def _close_abandoned_attempt() -> None:
            # A close of this logical call mid-response closes the provider
            # stream it was reading, as ``_LoopRun.close_abandoned_read`` does
            # one level up; one already closed or read to its end is a no-op.
            if inner_stream is not None:
                try:
                    await inner_stream.aclose()
                except Exception:
                    logger.debug("Failed to close abandoned provider stream", exc_info=True)

        logical_stream = ResponseStream(_updates(), finalizer=_finalizer).with_cleanup_hook(_close_abandoned_attempt)
        return logical_stream


class _LoopRun:
    """One ``ToolLoopLayer.get_response`` run: the two loop drivers and the steps they share.

    ``run_blocking`` and ``stream`` (assembled by ``finalize_stream``) drive
    the same iterations: send the prepped messages, land the response, close
    its cycle, run the landed calls as one batch and queue the next request —
    until no local call is left, or the iteration budget runs out and a final
    request without tools ends the run. They differ in how usage is counted
    and how a terminating batch ends the run; the streaming driver also
    yields between steps and withholds service-side handles ahead of the
    yields a consumer may stop at. Every step the two drive identically is a
    method here.

    The run's transcript state — echo registry, prepped and accumulated loop
    messages, tool ordinal, error and call counters, aggregated usage — lives
    for the run; the caller's history is read when a driver starts. Only the
    streaming driver writes ``_latest_usage``, ``_final_messages`` and
    ``_service_state_invalidated``, which ``finalize_stream`` reads, and
    ``_reading``, which ``close_abandoned_read`` reads and clears.

    Writes outside the run: ``tool_choice`` in the run's options, and
    ``tools`` through each tool batch (normalized per batch, so the next
    request offers the live list); the recorder's journal (landed responses,
    staged exchanges and their sealed results); the injection probe's drain;
    continuation state in the client kwargs, options and session through
    ``_update_continuation_state`` and ``_invalidate_service_continuation_state``;
    and the streaming driver's two session pre-clears. ``_WireCaller`` writes
    the options' ``continuation_token``.
    """

    def __init__(
        self,
        messages: Sequence[Message],
        *,
        options: dict[str, Any],
        client_kwargs: dict[str, Any],
        service_side: bool,
        session: AgentSession | None,
        recorder: LoopRecorder | None,
        injection_probe: ConsumedInjectionMessageProbe | None,
        pipeline: FunctionMiddlewarePipeline,
        additional_function_arguments: dict[str, Any],
        request_message_observer: Callable[[Sequence[Message]], None] | None,
        max_iterations: int,
        max_consecutive_errors: int,
        max_function_calls: int | None,
        tool_result_ceiling_tokens: int | None,
        trajectory: _LoopTrajectory,
        wire: _WireCaller,
        response_ready: ResponseReadyObserver | None,
    ) -> None:
        self._messages = messages
        self._options = options
        self._client_kwargs = client_kwargs
        self._service_side = service_side
        self._session = session
        self._recorder = recorder
        self._injection_probe = injection_probe
        self._pipeline = pipeline
        self._additional_function_arguments = additional_function_arguments
        self._max_iterations = max_iterations
        self._max_consecutive_errors = max_consecutive_errors
        self._max_function_calls = max_function_calls
        self._tool_result_ceiling_tokens = tool_result_ceiling_tokens
        self._trajectory = trajectory
        self._wire = wire
        self._response_ready_observer = response_ready
        self._response_format = options.get("response_format")
        # Per-run identity registry of every content the conversation
        # holds — seeded from caller history so a client echoing a
        # historical object cannot re-execute a past side-effecting
        # call; _land_response_contents removes reappearances as
        # echoes. ignore_usage: echo filtering never strips usage.
        self._echo_registry = WeakIdentityRegistry(ignore_usage=True)
        self._wire_request_observer = _wire_request_observer(self._echo_registry, request_message_observer)
        self._prepped_messages: list[Message] = []
        self._fcc_messages: list[Message] = []
        self._next_tool_ordinal = 0
        self._errors_in_a_row = 0
        self._total_function_calls = 0
        self._aggregated_usage: UsageDetails | None = None
        self._latest_usage: UsageDetails | None = None
        # Authoritative streamed transcript, recorded by each loop exit: the final response
        # reuses these already-assembled Message objects instead of re-merging
        # raw updates, so kernel-stamped call provenance survives streaming.
        # ``None`` means no loop exit completed — finalize_stream keeps the
        # ``from_updates`` merge as a defensive fallback.
        self._final_messages: list[Message] | None = None
        # Set by the streaming exits that withhold a service-stored response's
        # handles (termination, exhaustion strip): finalize_stream rebuilds
        # the response from raw updates, which still carry those continuation
        # ids, so it withholds them again.
        self._service_state_invalidated = False
        # The logical call the streaming driver is reading; None once it was
        # read to its end.
        self._reading: ResponseStream[ChatResponseUpdate, ChatResponse] | None = None

    # -- steps both drivers share ------------------------------------------

    def _start(self) -> None:
        for message in self._messages:
            for content in message.contents:
                self._echo_registry.register(content)
        self._prepped_messages = list(self._messages)

    def _land(self, response: ChatResponse) -> None:
        self._next_tool_ordinal = _land_response_contents(
            response,
            self._next_tool_ordinal,
            self._echo_registry,
            exchange_operation_id=self._trajectory.last_exchange_id,
        )

    def _take_consumed_injections(self) -> list[Message]:
        return self._injection_probe.take_consumed_messages() if self._injection_probe else []

    def _record_response_state(self, response: ChatResponse) -> None:
        _update_continuation_state(
            self._client_kwargs,
            response,
            session=self._session,
            options=self._options,
        )
        if self._recorder is not None:
            self._recorder.record_response(response)

    def _queue_consumed_injections(self, response: ChatResponse, consumed_injection_messages: list[Message]) -> None:
        if response.conversation_id is not None:
            self._prepped_messages = []
        else:
            self._prepped_messages.extend(consumed_injection_messages)

    async def _finish_cycle(self, response: ChatResponse) -> list[Content]:
        function_calls = _extract_function_calls(response)
        await self._trajectory.cycle_finished(response, function_calls)
        return function_calls

    async def _response_ready(self, response: ChatResponse) -> None:
        # The landed response, before any of its tools run and before the
        # run returns it: what its caller shows of it must come first.
        if self._response_ready_observer is not None:
            await self._response_ready_observer(response)

    async def _run_tool_batch(
        self, response: ChatResponse, function_calls: list[Content]
    ) -> tuple[list[Content], bool, str]:
        """Run the landed calls as one batch and fold their results into the loop messages.

        Returns the results, whether middleware terminated the loop, and
        ``"stop"`` when the error streak asks for a final turn without tools.
        """
        result_carrier_item_id = new_analytics_id()
        result_commits = (
            self._recorder.stage_exchange(
                response.messages,
                function_calls,
                result_carrier_item_id=result_carrier_item_id,
            )
            if self._recorder is not None
            else None
        )
        response_truncated = response.finish_reason == "length"
        with self._trajectory.exchange_scope():
            results, should_terminate, had_errors = await _execute_function_calls(
                function_calls=function_calls,
                result_commits=result_commits,
                tool_options=self._options,
                custom_args=self._additional_function_arguments,
                invocation_session=self._session,
                pipeline=self._pipeline,
                result_carrier_item_id=result_carrier_item_id,
                response_truncated=response_truncated,
                truncated_final_function_call_ids=(
                    _truncated_final_function_call_ids(response, function_calls) if response_truncated else None
                ),
                tool_result_ceiling_tokens=self._tool_result_ceiling_tokens,
                dispatch_observer=self._trajectory.mark_tool_operations_dispatched,
            )
        action, self._errors_in_a_row = _handle_function_call_results(
            response=response,
            function_call_results=results,
            fcc_messages=self._fcc_messages,
            echo_registry=self._echo_registry,
            errors_in_a_row=self._errors_in_a_row,
            had_errors=had_errors,
            max_errors=self._max_consecutive_errors,
            result_carrier_item_id=result_carrier_item_id,
            recorder=self._recorder,
        )
        self._total_function_calls += sum(1 for r in results if r.type == "function_result")
        return results, should_terminate, action

    def _tool_limit_fallback_text(self, action: str) -> str | None:
        """Return the final turn's fallback text when the batch just run hit a tool limit, else None.

        A hit limit ends the loop like iteration exhaustion: the collected
        results go out once more in the final turn without tools, whose calls
        are stripped, never run — a provider ignoring ``tool_choice="none"``
        cannot get another batch executed.
        """
        if action == "stop":
            return _CONSECUTIVE_ERRORS_FALLBACK_TEXT
        if self._max_function_calls is not None and self._total_function_calls >= self._max_function_calls:
            # Best-effort limit, checked after each parallel batch.
            logger.info(
                "Maximum function calls reached (%d/%d). Stopping further function calls for this request.",
                self._total_function_calls,
                self._max_function_calls,
            )
            return _MAX_FUNCTION_CALLS_FALLBACK_TEXT
        return None

    def _reset_required_tool_choice(self) -> None:
        # 'required' tool_choice resets after one iteration.
        if self._options.get("tool_choice") == "required" or (
            isinstance(self._options.get("tool_choice"), dict)
            and self._options.get("tool_choice", {}).get("mode") == "required"
        ):
            self._options["tool_choice"] = None

    def _queue_batch(self, response: ChatResponse) -> None:
        if response.conversation_id is not None:
            # Conversation APIs already hold the function-call message;
            # send only the new result message.
            self._prepped_messages.clear()
            if response.messages:
                self._prepped_messages.append(response.messages[-1])
        else:
            self._prepped_messages.extend(response.messages)

    def _log_iterations_exhausted(self, response: ChatResponse | None) -> None:
        if response is not None:
            logger.info(
                "Maximum iterations reached (%d). Requesting final response without tools.",
                self._max_iterations,
            )

    # -- blocking driver ----------------------------------------------------

    async def run_blocking(self) -> ChatResponse:
        interrupted = False
        try:
            return await self._blocking_iterations()
        except asyncio.CancelledError:
            # Nothing below may wait on the writer once the consumer
            # is gone: the settlement queues its lines instead.
            interrupted = True
            self._trajectory.abort(self._wire.cancel_outcome())
            raise
        except Exception:
            self._trajectory.abort(ExchangeOutcome.ERROR)
            raise
        finally:
            await self._trajectory.settle_undispatched_tool_operations(queued=interrupted)

    async def _blocking_iterations(self) -> ChatResponse:
        self._start()
        response: ChatResponse | None = None
        tail_cycle_index = self._max_iterations
        fallback_text = _MAX_ITERATIONS_FALLBACK_TEXT

        for cycle_index in range(self._max_iterations):
            await self._trajectory.cycle_started(cycle_index, tools_offered=bool(self._options.get("tools")))
            response = await self._wire.blocking_response(
                self._prepped_messages,
                request_message_observer=self._wire_request_observer,
            )
            self._land(response)
            consumed_injection_messages = self._take_consumed_injections()
            response.latest_usage_details = response.usage_details
            self._aggregated_usage = add_usage_details(self._aggregated_usage, response.usage_details)
            self._record_response_state(response)
            self._queue_consumed_injections(response, consumed_injection_messages)

            function_calls = await self._finish_cycle(response)
            await self._response_ready(response)
            if not (function_calls and self._options.get("tools")):
                _prepend_fcc_messages(response, self._fcc_messages)
                response.usage_details = self._aggregated_usage
                return _clear_internal_conversation_id(response)

            _results, should_terminate, action = await self._run_tool_batch(response, function_calls)
            if should_terminate:
                # Middleware termination: return the current response
                # (tool results already appended) without an fcc
                # prepend. Streaming termination instead returns the
                # accumulated transcript because earlier updates were delivered.
                if self._service_side:
                    # No further request ever posts this batch's
                    # results, so the service transcript behind the
                    # mirrored handle keeps its calls unanswered.
                    # Record and withhold the handles like the
                    # exhaustion strip does; the marker lets the
                    # agent post-hook install the local fallback.
                    _invalidate_service_continuation_state(response, self._session)
                response.usage_details = self._aggregated_usage
                return _clear_internal_conversation_id(response)
            self._queue_batch(response)
            limit_text = self._tool_limit_fallback_text(action)
            if limit_text is not None:
                tail_cycle_index, fallback_text = cycle_index + 1, limit_text
                break
            self._reset_required_tool_choice()
        else:
            self._log_iterations_exhausted(response)

        # Loop exhausted or a tool limit hit: final model call with
        # tool_choice="none" so the model produces plain text instead of
        # orphaned function calls.
        self._options["tool_choice"] = "none"
        await self._trajectory.cycle_started(tail_cycle_index, tools_offered=bool(self._options.get("tools")))
        # A tail that shows nothing gets the fallback text below, so validation lets it through.
        response = await self._wire.blocking_response(
            self._prepped_messages,
            request_message_observer=self._wire_request_observer,
            exhaustion_tail=True,
        )
        self._land(response)
        # Counted before the strip below: a provider that ignored
        # ``tool_choice="none"`` did ask for calls, and a tail that
        # reported none would read as a compliant one.
        await self._finish_cycle(response)
        stripped = _strip_unexecutable_calls_from_response(response)
        _ensure_exhaustion_fallback_response(response, fallback_text)
        await self._response_ready(response)
        # Gate on the resolved storage mode, not response metadata: under
        # conversation storage the service holds the stripped calls even
        # when the parsed response failed to carry the handle, and a
        # client-side-storage response's metadata must survive untouched.
        if stripped and self._service_side:
            _invalidate_service_continuation_state(response, self._session)
        self._take_consumed_injections()
        response.latest_usage_details = response.usage_details
        self._aggregated_usage = add_usage_details(self._aggregated_usage, response.usage_details)
        self._record_response_state(response)
        response.usage_details = self._aggregated_usage
        _prepend_fcc_messages(response, self._fcc_messages)
        return _clear_internal_conversation_id(response)

    # -- streaming driver ---------------------------------------------------

    def _count_streamed_usage(self, response: ChatResponse) -> None:
        latest_usage = response.latest_usage_details
        if latest_usage is None:
            latest_usage = response.usage_details
        response.latest_usage_details = latest_usage
        self._latest_usage = latest_usage
        if latest_usage is not None:
            self._aggregated_usage = add_usage_details(self._aggregated_usage, latest_usage)

    async def stream(self) -> AsyncIterable[ChatResponseUpdate]:
        # One generator end to end: every exit but the normal return closes
        # the trajectory bookkeeping it opened. A consumer abandoning the
        # stream lands here as GeneratorExit, an interrupt as CancelledError.
        # Delegating the body to an inner generator would lose the
        # GeneratorExit: closing this generator does not close one it is
        # iterating, so the inner body would see it only at garbage collection.
        # For the same reason the logical call it is reading is closed apart,
        # by ``close_abandoned_read``.
        interrupted = False
        try:
            self._start()
            response: ChatResponse | None = None
            tail_cycle_index = self._max_iterations
            fallback_text = _MAX_ITERATIONS_FALLBACK_TEXT

            for cycle_index in range(self._max_iterations):
                await self._trajectory.cycle_started(cycle_index, tools_offered=bool(self._options.get("tools")))
                # Echoed conversation-held objects must never reach update
                # assembly (merges would launder their identity past the
                # memo). Delivered on the REQUEST path so the middleware
                # pipeline attaches it to the stream its final handler
                # resolves — beneath every middleware, which is the only
                # placement that crosses semantic stream proxies and
                # re-attaches whenever a middleware calls on again. The provider
                # finalizer, every result hook, and the yielded updates all
                # observe one echo-free sequence, so nothing is re-assembled
                # behind a hook's back.
                echo_filter = functools.partial(
                    _strip_echoed_update,
                    echo_registry=self._echo_registry,
                )
                logical_stream = self._wire.streaming_response(
                    self._prepped_messages,
                    stream_update_filter=echo_filter,
                    request_message_observer=self._wire_request_observer,
                )
                self._reading = logical_stream
                async for update in logical_stream:
                    yield update
                self._reading = None
                # Triggers the inner stream's finalizer and result hooks (the
                # wire client's intermediate-text hook runs before tool
                # extraction below — "hook before tool detection" ordering).
                response = await logical_stream.get_final_response()
                _remove_echo_emptied_message_shells(response)
                self._land(response)
                _record_stream_fragment_identities(logical_stream, self._echo_registry)
                self._count_streamed_usage(response)
                consumed_injection_messages = self._take_consumed_injections()
                self._record_response_state(response)
                self._queue_consumed_injections(response, consumed_injection_messages)

                # Continue only for unresolved local function calls.
                function_calls = await self._finish_cycle(response)
                await self._response_ready(response)
                if not (function_calls and self._options.get("tools")):
                    self._final_messages = [*self._fcc_messages, *response.messages]
                    return

                results, should_terminate, action = await self._run_tool_batch(response, function_calls)
                limit_text = None if should_terminate else self._tool_limit_fallback_text(action)
                if should_terminate and self._service_side:
                    # No further request ever posts this batch's results, so
                    # the service transcript behind the mirrored handle keeps
                    # its calls unanswered. Record and withhold the handles
                    # like the exhaustion strip does — ahead of the
                    # last-iteration clear below, which would drop the
                    # session's copy unrecorded — and raise the run flag
                    # so ``finalize_stream`` stamps the verdict on the
                    # assembled response for the agent post-hook.
                    self._service_state_invalidated = True
                    _invalidate_service_continuation_state(response, self._session)
                elif (
                    (limit_text is not None or cycle_index + 1 == self._max_iterations)
                    and self._service_side
                    and self._session is not None
                ):
                    # Last batch (iterations exhausted or a tool limit hit):
                    # the yield below is the final suspension point before
                    # the exhaustion tail, and these results are not posted
                    # to the service until the tail request lands.
                    # A consumer closing at that yield must not inherit the
                    # mirrored handle — the service transcript behind it still
                    # holds this batch's unanswered calls. The finalized tail
                    # restores the fresh handle via
                    # ``_update_continuation_state``.
                    self._session.service_session_id = None
                # Synthesized tool-result update so from_updates can rebuild the
                # full transcript.
                yield ChatResponseUpdate(contents=results, role="tool")
                if should_terminate:
                    # This batch (calls + results) was already folded into
                    # _fcc_messages by _handle_function_call_results, so the
                    # assembly is the accumulated transcript with no tail.
                    # Deliberate asymmetry with the non-streaming termination
                    # return (only the terminating response, no prepend):
                    # each path keeps its existing transcript shape.
                    self._final_messages = list(self._fcc_messages)
                    return
                self._queue_batch(response)
                if limit_text is not None:
                    tail_cycle_index, fallback_text = cycle_index + 1, limit_text
                    break
                self._reset_required_tool_choice()
            else:
                self._log_iterations_exhausted(response)

            # Loop exhausted or a tool limit hit: final non-tool streaming turn.
            self._options["tool_choice"] = "none"
            if self._service_side and self._session is not None:
                # The tail request consumes the mirrored handle: the moment it
                # lands, the service transcript behind that handle holds this
                # run's still-unanswered calls, so a consumer abandoning the
                # tail must not inherit it (the tail updates below are marked
                # against eager re-mirroring for the same reason). Successful
                # finalization restores the fresh handle via
                # ``_update_continuation_state`` — or withholds it when the
                # strip invalidates the service state.
                self._session.service_session_id = None
            echo_filter = functools.partial(
                _strip_echoed_update,
                echo_registry=self._echo_registry,
            )
            await self._trajectory.cycle_started(tail_cycle_index, tools_offered=bool(self._options.get("tools")))
            final_logical_stream = self._wire.streaming_response(
                self._prepped_messages,
                stream_update_filter=echo_filter,
                request_message_observer=self._wire_request_observer,
                exhaustion_tail=True,
            )
            tail_stripped_call = False
            self._reading = final_logical_stream
            async for update in final_logical_stream:
                kept = _strip_unexecutable_calls_from_update(update, suppress_finish_reason=tail_stripped_call)
                if kept is not update:
                    # A call was stripped: from here on no provider
                    # finish_reason may cross the stream — providers commonly
                    # emit the call delta and the finish-reason chunk
                    # separately, and a first-terminal consumer would stop at
                    # that later chunk before the corrective final update
                    # re-emits the corrected reason.
                    tail_stripped_call = True
                if kept is not None:
                    # Ephemeral marker (never serialized): eager continuation
                    # mirrors must skip tail updates. The run is ending, so a
                    # mid-stream mirror has no disconnect-recovery value left,
                    # and a consumer abandoning the stream mid-tail would
                    # otherwise keep the session pointed at a service
                    # transcript whose stripped calls will never be answered;
                    # the tail's own verdict (post-hook restore or
                    # invalidation) is the sole writer once the stream ends.
                    kept.__dict__["_chrys_exhaustion_tail_update"] = True
                    yield kept
            self._reading = None
            final_response = await final_logical_stream.get_final_response()
            _remove_echo_emptied_message_shells(final_response)
            self._land(final_response)
            await self._finish_cycle(final_response)
            _record_stream_fragment_identities(final_logical_stream, self._echo_registry)
            stripped = _strip_unexecutable_calls_from_response(final_response)
            fallback_content = _ensure_exhaustion_fallback_response(final_response, fallback_text)
            # Invalidate BEFORE the fallback yield: a yield suspends the
            # generator, and a consumer that stops right after the fallback
            # would otherwise leave the session pointing at the stale service
            # transcript (the eager transform already wrote it mid-stream).
            # Gate on the resolved storage mode, not response metadata: under
            # conversation storage the service holds the stripped calls even
            # when the parsed response failed to carry the handle, and a
            # client-side-storage response's metadata must survive untouched.
            if stripped and self._service_side:
                self._service_state_invalidated = True
                _invalidate_service_continuation_state(final_response, self._session)
            if fallback_content is not None:
                # Streamed consumers saw no visible content either (the yield
                # loop strips the same calls); surface the fallback text there
                # too, not only on the assembled response. The stripped tail
                # updates carry no terminal reason (the update strip clears
                # it), so this update supplies the stream's sole terminal
                # "stop" — the fallback ends the run, there is no tool work
                # left to route. It carries the very content the response
                # holds, so what a stream observer records on it lands too.
                yield ChatResponseUpdate(role="assistant", contents=[fallback_content], finish_reason="stop")
            elif stripped and final_response.finish_reason:
                # No fallback (the final kept visible text), but the strip
                # suppressed every provider reason after the first stripped
                # call and normalized the wire's "tool_calls" to "stop":
                # streamed consumers need the corrected signal — whatever
                # reason the assembled response settled on, "stop" or a
                # non-stop reason like "length" — and it must arrive only
                # after every stripped update.
                yield ChatResponseUpdate(role="assistant", contents=[], finish_reason=final_response.finish_reason)
            await self._response_ready(final_response)
            self._count_streamed_usage(final_response)
            self._take_consumed_injections()
            self._record_response_state(final_response)
            self._final_messages = [*self._fcc_messages, *final_response.messages]
        except asyncio.CancelledError:
            # Nothing below may wait on the writer once the consumer is
            # gone: the settlement queues its lines instead.
            interrupted = True
            self._trajectory.abort(self._wire.cancel_outcome())
            raise
        except GeneratorExit:
            interrupted = True
            self._trajectory.abort(ExchangeOutcome.ABANDONED)
            raise
        except Exception:
            self._trajectory.abort(ExchangeOutcome.ERROR)
            raise
        finally:
            await self._trajectory.settle_undispatched_tool_operations(queued=interrupted)

    async def close_abandoned_read(self) -> None:
        """Cleanup hook of the run's stream: close the logical call it was reading.

        A logical call is left unread when the consumer closes the run's
        stream mid-response, or when the run fails between two pulls of it.
        On every other exit it was read to its end, or it ended by itself
        (error, cancellation) and closing it again has no effect. Closing it
        closes the provider stream under it, so that stream's own cleanup
        hooks (usage, telemetry) run before the close or the failure reaches
        the consumer. The hook runs after ``stream`` exited and aborted its
        trajectory, never from a ``GeneratorExit`` handler: finalization
        closes each abandoned generator in a task of its own, and a handler
        closing another generator would collide with that generator's own
        close.
        """
        reading, self._reading = self._reading, None
        if reading is not None:
            try:
                await reading.aclose()
            except Exception:
                logger.debug("Failed to close an abandoned logical stream", exc_info=True)

    def finalize_stream(self, updates: Sequence[ChatResponseUpdate]) -> ChatResponse:
        # Inner result hooks already ran via get_final_response; do not run them again.
        response = ChatResponse.from_updates(updates, output_format_type=self._response_format)
        if self._final_messages is not None:
            # The loop is the sole reconstruction authority: keep
            # ``from_updates`` for response-level fields, but reuse the
            # loop's assembled Message objects. Re-merging raw fragments
            # here would rebuild multi-fragment function calls as new
            # Content objects, dropping the kernel-stamped invocation
            # ordinal, tool kind/context, and folded result metadata —
            # and could glue a boundary-shaped delta onto the synthesized
            # tool message. Structured-output ``.value`` parses lazily
            # from these swapped messages; their text is identical.
            response.messages = list(self._final_messages)
        response.usage_details = self._aggregated_usage
        response.latest_usage_details = self._latest_usage
        if self._service_state_invalidated:
            # ``from_updates`` restored the handles from raw updates; the
            # run withheld them (exhaustion strip or middleware termination)
            # because the service-side transcript holds calls it never sees
            # answered.
            response.conversation_id = None
            response.response_id = None
            response._chrys_service_state_invalidated = True
        return response


# --------------------------------------------------------------------------- #
# ToolLoopLayer
# --------------------------------------------------------------------------- #


class ToolLoopLayer:
    """Composition-style tool-invocation loop over an inner chat layer.

    Constructor settings bound iteration and tool execution. Clients that
    need a single model call can use the inner chat layer directly.

    Per-run state arrives via ``client_kwargs``: ``session`` (continuation
    bookkeeping target), ``loop_recorder`` (chrys interrupt-recovery
    recorder) and the :data:`RESPONSE_READY_KWARG` observer, among others,
    are popped here; everything else flows to the inner layer
    untouched, with continuation ids written back into the same dict between
    iterations.
    """

    def __init__(
        self,
        inner: ChatMiddlewareLayer,
        *,
        middleware: FunctionMiddleware | Sequence[FunctionMiddleware] | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
        max_function_calls: int | None = None,
        tool_result_ceiling_tokens: int | None = None,
    ) -> None:
        self.inner = inner
        split = split_middleware(middleware)
        if split.chat:
            raise TypeError(
                "ToolLoopLayer constructor middleware must be FunctionMiddleware; "
                f"chat middleware belongs to the inner ChatMiddlewareLayer, got {split.chat!r}"
            )
        self.function_middleware: list[FunctionMiddleware] = split.function
        self.max_iterations = max_iterations
        self.max_consecutive_errors = max_consecutive_errors
        self.max_function_calls = max_function_calls
        self.tool_result_ceiling_tokens = tool_result_ceiling_tokens

    def __getattr__(self, name: str) -> Any:
        # Delegate unknown attributes to the inner layer so agent-facing client
        # surface (model, STORES_BY_DEFAULT, ...) resolves through the stack.
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    async def aclose(self) -> None:
        await self.inner.aclose()

    def get_response(
        self,
        messages: Sequence[Message],
        *,
        stream: bool = False,
        options: Mapping[str, Any] | None = None,
        middleware: ChatMiddleware | FunctionMiddleware | Sequence[ChatMiddleware | FunctionMiddleware] | None = None,
        compaction_strategy: Any = None,
        tokenizer: Any = None,
        function_invocation_kwargs: Mapping[str, Any] | None = None,
        client_kwargs: Mapping[str, Any] | None = None,
        request_message_observer: Callable[[Sequence[Message]], None] | None = None,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        """Build a lazy model/tool run.

        ``stream=False`` returns an un-awaited coroutine; ``stream=True`` returns
        a ``ResponseStream`` synchronously. Execution begins when the result is driven.
        The run's settings are resolved here; the caller's messages are read
        when the run starts.
        """
        # Merge the middleware kwarg with the
        # client_kwargs channel, split chat vs function, pop per-run state.
        effective_client_kwargs = dict(client_kwargs) if client_kwargs is not None else {}
        if middleware is not None:
            existing = effective_client_kwargs.get("middleware")
            effective_client_kwargs["middleware"] = [
                *_as_middleware_list(existing),
                *_as_middleware_list(middleware),
            ]
        runtime_split = split_middleware(effective_client_kwargs.pop("middleware", None))
        pipeline = FunctionMiddlewarePipeline(*self.function_middleware, *runtime_split.function)
        per_call_chat: list[ChatMiddleware] = runtime_split.chat

        raw_session = effective_client_kwargs.pop("session", None)
        invocation_session: AgentSession | None = raw_session if isinstance(raw_session, AgentSession) else None
        raw_recorder = effective_client_kwargs.pop("loop_recorder", None)
        recorder: LoopRecorder | None = raw_recorder if isinstance(raw_recorder, LoopRecorder) else None
        raw_injection_probe = effective_client_kwargs.pop("consumed_injection_message_probe", None)
        injection_probe = (
            raw_injection_probe if isinstance(raw_injection_probe, ConsumedInjectionMessageProbe) else None
        )
        raw_wire_retry_policy = effective_client_kwargs.pop("wire_retry_policy", None)
        raw_token_observer = effective_client_kwargs.pop("continuation_token_observer", None)
        continuation_token_observer: Callable[[Any], None] | None = (
            raw_token_observer if callable(raw_token_observer) else None
        )
        raw_response_ready = effective_client_kwargs.pop(RESPONSE_READY_KWARG, None)
        response_ready: ResponseReadyObserver | None = raw_response_ready if callable(raw_response_ready) else None
        raw_trajectory = effective_client_kwargs.pop(TRAJECTORY_CONTEXT_KWARG, None)
        trajectory: TrajectoryContext | None = raw_trajectory if isinstance(raw_trajectory, TrajectoryContext) else None

        filtered_kwargs = effective_client_kwargs

        additional_function_arguments = dict(function_invocation_kwargs) if function_invocation_kwargs else {}
        if options and (additional_opts := options.get("additional_function_arguments")):
            additional_function_arguments.update(additional_opts)

        mutable_options: dict[str, Any] = dict(options) if options else {}
        # Tool-invocation-only key; not recognized by chat service APIs.
        mutable_options.pop("additional_function_arguments", None)
        # Run-local mutable tools list (progressive tool exposure): fresh list so
        # the caller's container is never mutated, same object shared with the
        # model and the per-batch tool map.
        if mutable_options.get("tools"):
            mutable_options["tools"] = normalize_tools(mutable_options["tools"])

        try:
            stores_by_default = bool(self.STORES_BY_DEFAULT)
        except AttributeError:
            stores_by_default = False
        try:
            force_stateless = bool(self.FORCES_STATELESS)
        except AttributeError:
            force_stateless = False
        storage = resolve_storage_mode_and_handles(
            mutable_options,
            stores_by_default=stores_by_default,
            client_kwargs=filtered_kwargs,
            force_stateless=force_stateless,
        )
        wire_retry_policy = (
            cast(WireRetryPolicy, raw_wire_retry_policy)
            if raw_wire_retry_policy is not None and not storage.service_side
            else None
        )

        run_trajectory = _LoopTrajectory(trajectory, retry_policy=wire_retry_policy)
        wire = _WireCaller(
            self.inner,
            options=mutable_options,
            client_kwargs=filtered_kwargs,
            chat_middleware=per_call_chat,
            compaction_strategy=compaction_strategy,
            tokenizer=tokenizer,
            policy=wire_retry_policy,
            recorder=recorder,
            injection_probe=injection_probe,
            continuation_token_observer=continuation_token_observer,
            trajectory=run_trajectory,
        )
        run = _LoopRun(
            messages,
            options=mutable_options,
            client_kwargs=filtered_kwargs,
            service_side=storage.service_side,
            session=invocation_session,
            recorder=recorder,
            injection_probe=injection_probe,
            pipeline=pipeline,
            additional_function_arguments=additional_function_arguments,
            request_message_observer=request_message_observer,
            max_iterations=self.max_iterations,
            max_consecutive_errors=self.max_consecutive_errors,
            max_function_calls=self.max_function_calls,
            tool_result_ceiling_tokens=self.tool_result_ceiling_tokens,
            trajectory=run_trajectory,
            wire=wire,
            response_ready=response_ready,
        )
        if not stream:
            return run.run_blocking()
        return ResponseStream(run.stream(), finalizer=run.finalize_stream).with_cleanup_hook(run.close_abandoned_read)


async def _resolve_response(value: Any) -> ChatResponse:
    """Resolve the inner layer's non-streaming return to a ``ChatResponse``.

    The inner :class:`~.middleware.ChatMiddlewareLayer` returns an un-awaited
    coroutine for ``stream=False``; a plain client returning a response object
    directly is accepted too.
    """
    if inspect.isawaitable(value):
        return await value
    return value
