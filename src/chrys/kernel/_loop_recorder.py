# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interrupt-recovery journal for the model/tool loop.

``LoopRecorder`` keeps what a run has done so far, so an interrupted turn can
be saved and resumed: the pre-call message snapshots, the service-side loop
messages, and an ordinal-slot journal of every landed call batch. Tool
execution fills a slot only through the ``_ResultCommit`` handle the loop
stages for it.

``_message_snapshot`` is the loop's wrapper-copy primitive for landed
messages: it copies the Message wrapper and shares its Content objects.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping, Sequence
from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chrys.foundation.recovery import RecoveryPersistOutcome
from chrys.foundation.tool_invocation_order import read_tool_invocation_order
from chrys.foundation.tool_result_metadata import (
    TOOL_FAILED_METADATA_KEY,
    TOOL_INTERRUPTED_METADATA_KEY,
    TOOL_POST_PROCESSING_INTERRUPTED_METADATA_KEY,
    TOOL_RESULT_METADATA_KEY,
)
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.foundation.trajectory_timing import TRAJECTORY_TIMING_KEY

from ._content import Content
from ._tool_execution import _is_actionable_function_call, _result_additional_properties, _tool_trajectory_timing
from ._types import ChatResponse, Message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from .middleware import FunctionInvocationContext

# Log lines keep the loop's logger name: log configuration and tests filter on it.
logger = logging.getLogger("chrys.kernel.loop")


def _contains_identity(messages: list[Message], target: Message) -> bool:
    return any(message is target for message in messages)


def _has_function_call(message: Message) -> bool:
    return any(_is_actionable_function_call(content) for content in message.contents)


def _message_snapshot(message: Message, contents: list[Content]) -> Message:
    """Loop-owned shallow copy of a message wrapper holding *contents*.

    The wrapper and its two containers (``contents`` list,
    ``additional_properties`` dict) are copied; the Content objects are NOT —
    their identity is the echo memo's currency and must stay shared. Landing
    retains these snapshots instead of client-minted wrappers
    (``_land_response_contents``); the request direction of wrapper aliasing
    is covered by ``_wire_message_view``.
    """
    snapshot = copy(message)
    snapshot.contents = contents
    snapshot.additional_properties = dict(message.additional_properties)
    # Echo-strip provenance is assembly-local. A mixed echo+fresh message is
    # retained, but its snapshot must not carry the marker into later calls.
    snapshot._chrys_echo_content_stripped = False
    return snapshot


@dataclass(slots=True)
class _PendingExchangeSlot:
    ordinal: int
    function_call: Content
    call_id: str | None
    result: Content | None = None
    fill_kind: str = ""


@dataclass(slots=True)
class _PendingExchange:
    response_messages: tuple[Message, ...]
    slots: tuple[_PendingExchangeSlot, ...]
    result_carrier_item_id: str
    projection_messages: tuple[Message, ...] | None = None

    @property
    def answered_count(self) -> int:
        return sum(slot.result is not None for slot in self.slots)


@dataclass(frozen=True, slots=True)
class _SealedExchange:
    messages: tuple[Message, ...]
    answered_count: int


class _ResultCommit:
    """Synchronous, invocation-bound writer for one pending exchange slot."""

    def __init__(self, recorder: LoopRecorder, exchange: _PendingExchange, slot: _PendingExchangeSlot) -> None:
        self._recorder = recorder
        self._exchange = exchange
        self._slot = slot

    @property
    def has_raw_result(self) -> bool:
        return self._slot.fill_kind == "raw"

    def commit_raw(self, result: Content) -> None:
        self._recorder._fill_slot(self._exchange, self._slot, result, fill_kind="raw")

    def commit_final(self, result: Content) -> None:
        self._recorder._fill_slot(self._exchange, self._slot, result, fill_kind="final", upgrade_raw=True)

    def commit_interrupted(
        self,
        function_call: Content,
        invocation_context: FunctionInvocationContext | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._recorder._interrupt_slot(
            self._exchange,
            self._slot,
            function_call,
            invocation_context,
            metadata,
        )


@dataclass(frozen=True, slots=True)
class LoopRecorderSnapshot:
    """Retry snapshot of non-journal :class:`LoopRecorder` capture state."""

    initial_count: int | None
    captured: tuple[Message, ...] | None
    service_loop_messages: tuple[Message, ...]
    last_checkpoint_key: tuple[int, str] | None


class LoopRecorder:
    """Records tool-loop messages for interrupt recovery.

    Successor to the chat-middleware-based loop-capture middleware: the loop
    feeds it directly (:meth:`record_pre_call` before each wire call,
    :meth:`record_response` after each parsed response) instead of observing
    the per-call middleware pipeline. The consumer surface
    (:attr:`loop_messages`, :meth:`reset`, :attr:`on_checkpoint`) and the
    capture semantics are unchanged.

    On early termination (e.g. hard-cancel ``CancelledError``) only the last
    iteration's messages appear in the run result — previous iterations live
    only in the loop's growing ``prepped_messages`` list. The pre-call
    snapshot preserves them so the engine can merge completed iterations into
    session state.

    ``message_hasher`` turns the newest captured message into a stable payload
    string for checkpoint suppression (chrys injects a ``serialize_message``
    JSON wrapper; the kernel cannot import chrys). Defaults to ``repr``.

    **Reset contract**: :meth:`reset` must be called before each fresh run.

    **Delivery**: per-run via ``client_kwargs["loop_recorder"]`` — parallel
    sub-agent invocations share one client stack, so the recorder must never
    be attached to the layer itself.
    """

    def __init__(
        self,
        *,
        capture_service_loop_messages: bool = False,
        on_checkpoint: Callable[[], Coroutine[Any, Any, None]] | None = None,
        on_pre_wire_barrier: Callable[[], Awaitable[RecoveryPersistOutcome]] | None = None,
        on_result_checkpoint: Callable[[], Coroutine[Any, Any, None]] | None = None,
        message_hasher: Callable[[Message], str] | None = None,
    ) -> None:
        self._capture_service_loop_messages = capture_service_loop_messages
        self._on_result_checkpoint = on_result_checkpoint if on_result_checkpoint is not None else on_checkpoint
        self._on_pre_wire_barrier = on_pre_wire_barrier
        self._message_hasher: Callable[[Message], str] = message_hasher if message_hasher is not None else repr
        self._initial_count: int | None = None
        self._captured: list[Message] | None = None
        self._service_loop_messages: list[Message] = []
        self._last_checkpoint_key: tuple[int, str] | None = None
        self._sealed_exchanges: list[_SealedExchange] = []
        self._pending_exchange: _PendingExchange | None = None
        self._landed_response: tuple[Message, ...] | None = None
        self._committed_count = 0
        self._barrier_degraded = False
        self._barrier_unconfigured = False
        self._barrier_warned = False

    def reset(self) -> None:
        """Clear captured state for a new run."""
        self._initial_count = None
        self._captured = None
        self._service_loop_messages = []
        self._last_checkpoint_key = None
        self._sealed_exchanges = []
        self._pending_exchange = None
        self._landed_response = None
        self._committed_count = 0
        self._barrier_degraded = False
        self._barrier_warned = False

    def snapshot(self) -> LoopRecorderSnapshot:
        """Capture recorder state for an outer provider retry."""
        return LoopRecorderSnapshot(
            initial_count=self._initial_count,
            captured=None if self._captured is None else tuple(self._captured),
            service_loop_messages=tuple(self._service_loop_messages),
            last_checkpoint_key=self._last_checkpoint_key,
        )

    def restore(self, snapshot: LoopRecorderSnapshot) -> None:
        """Restore capture state while preserving every answered journal slot."""
        self._initial_count = snapshot.initial_count
        self._captured = None if snapshot.captured is None else list(snapshot.captured)
        self._service_loop_messages = list(snapshot.service_loop_messages)
        self._last_checkpoint_key = snapshot.last_checkpoint_key
        # The retried attempt sends its request again.
        self._landed_response = None
        pending = self._pending_exchange
        if pending is not None and not any(slot.fill_kind in ("raw", "final") for slot in pending.slots):
            self._pending_exchange = None

    @property
    def on_checkpoint(self) -> Callable[[], Coroutine[Any, Any, None]] | None:
        """Compatibility alias for the best-effort checkpoint callback."""
        return self._on_result_checkpoint

    @on_checkpoint.setter
    def on_checkpoint(self, callback: Callable[[], Coroutine[Any, Any, None]] | None) -> None:
        self._on_result_checkpoint = callback

    @property
    def on_pre_wire_barrier(self) -> Callable[[], Awaitable[RecoveryPersistOutcome]] | None:
        """Strict recovery barrier invoked before a post-commit provider call."""
        return self._on_pre_wire_barrier

    @on_pre_wire_barrier.setter
    def on_pre_wire_barrier(
        self,
        callback: Callable[[], Awaitable[RecoveryPersistOutcome]] | None,
    ) -> None:
        self._on_pre_wire_barrier = callback
        self._barrier_unconfigured = False

    @property
    def on_result_checkpoint(self) -> Callable[[], Coroutine[Any, Any, None]] | None:
        """Best-effort checkpoint callback kicked after slot fills and snapshots."""
        return self._on_result_checkpoint

    @on_result_checkpoint.setter
    def on_result_checkpoint(self, callback: Callable[[], Coroutine[Any, Any, None]] | None) -> None:
        self._on_result_checkpoint = callback

    @property
    def committed_count(self) -> int:
        """Number of answered ordinal slots in the current outer pass."""
        return self._committed_count

    @property
    def initial_count(self) -> int | None:
        """Message count of the first wire call, or ``None`` before any call."""
        return self._initial_count

    @property
    def captured_count(self) -> int | None:
        """Size of the newest pre-call snapshot, or ``None`` before any call."""
        return None if self._captured is None else len(self._captured)

    @property
    def landed_response(self) -> tuple[Message, ...] | None:
        """The newest request's response messages as the loop landed them; ``None`` while it is in flight.

        Their Content objects are the ones history keeps, so they tell that request's own exchange apart
        from earlier ones reusing its call ids.
        """
        return self._landed_response

    @property
    def loop_messages(self) -> list[Message] | None:
        """Messages from completed tool loop iterations, or ``None``.

        Returns the assistant + tool messages that the tool loop accumulated
        from **previous** iterations.  Returns ``None`` if no multi-iteration
        loop occurred (single iteration or no tool calls).
        """
        captured_delta: list[Message] = []
        if self._captured is not None and self._initial_count is not None and len(self._captured) > self._initial_count:
            captured_delta = self._captured[self._initial_count :]

        base_messages = self._service_loop_messages or captured_delta
        journal: list[tuple[set[int], tuple[Message, ...]]] = [
            (
                {id(content) for message in exchange.messages for content in message.contents},
                exchange.messages,
            )
            for exchange in self._sealed_exchanges
        ]
        if self._pending_exchange is not None and self._pending_exchange.answered_count:
            pending_projection = tuple(self._project_pending_exchange(self._pending_exchange))
            pending_owned = {
                id(content) for message in self._pending_exchange.response_messages for content in message.contents
            }
            pending_owned.update(id(slot.result) for slot in self._pending_exchange.slots if slot.result is not None)
            journal.append((pending_owned, pending_projection))

        candidates: list[Message] = []
        inserted: set[int] = set()
        all_owned = set().union(*(owned for owned, _projection in journal)) if journal else set()
        for message in base_messages:
            message_ids = {id(content) for content in message.contents}
            for index, (owned, projection) in enumerate(journal):
                if index not in inserted and message_ids.intersection(owned):
                    candidates.extend(projection)
                    inserted.add(index)
            remaining = [content for content in message.contents if id(content) not in all_owned]
            if remaining:
                candidates.append(
                    message if len(remaining) == len(message.contents) else _message_snapshot(message, remaining)
                )
        for index, (_owned, projection) in enumerate(journal):
            if index not in inserted:
                candidates.extend(projection)
        deduped = self._dedupe_projected_messages(candidates)
        return deduped or None

    async def record_pre_call(self, messages: list[Message]) -> None:
        """Snapshot the prepped message list before a wire call."""
        self._landed_response = None
        if self._initial_count is None:
            self._initial_count = len(messages)
        prev_len = len(self._captured) if self._captured else 0
        # Copy to avoid aliasing mutations of the loop's growing list.
        self._captured = list(messages)
        # Responses store=true service-side continuations replace the growing
        # local prepped list with compact assistant/tool payloads. Preserve
        # those too so pause-time recovery can replay locally after dropping
        # the service id. The mode is configured by chrys from the model
        # profile/options; ``ChatResponse.conversation_id`` alone is provider
        # metadata and is not a reliable discriminator.
        if self._capture_service_loop_messages and self._service_loop_messages:
            for msg in messages:
                if not _contains_identity(self._service_loop_messages, msg) and msg.role in ("assistant", "tool"):
                    self._service_loop_messages.append(msg)
        logger.debug(
            "LoopRecorder: initial=%d prev=%d now=%d delta=%d",
            self._initial_count,
            prev_len,
            len(self._captured),
            len(self._captured) - self._initial_count,
        )
        barrier_persisted = await self._run_pre_wire_barrier()
        if self._on_result_checkpoint is None:
            return
        # A persisted barrier already made this pre-call state durable; the
        # best-effort checkpoint would only build and write it again. Suppression
        # still runs first: it records this prefix as checkpointed, so a retry of
        # the same request stays suppressed after a later barrier failure.
        if self._should_suppress_checkpoint() or barrier_persisted:
            return
        await self._on_result_checkpoint()

    def record_response(self, response: ChatResponse) -> None:
        """Record assistant function-call messages from a parsed response.

        The loop owns the parsed ``ChatResponse`` for both stream modes, so the
        stream ``result_hook`` side channel of the middleware era is gone.
        """
        self._landed_response = tuple(response.messages)
        if not self._capture_service_loop_messages:
            return
        for msg in response.messages:
            if not _contains_identity(self._service_loop_messages, msg) and _has_function_call(msg):
                self._service_loop_messages.append(msg)

    def stage_exchange(
        self,
        response_messages: Sequence[Message],
        function_calls: Sequence[Content],
        *,
        result_carrier_item_id: str,
    ) -> tuple[_ResultCommit, ...]:
        """Reserve ordinal slots for one landed call batch."""
        slots: list[_PendingExchangeSlot] = []
        for function_call in function_calls:
            ordinal = read_tool_invocation_order(function_call.additional_properties)
            if ordinal is None:
                raise ValueError("Landed function call is missing its invocation ordinal.")
            slots.append(
                _PendingExchangeSlot(
                    ordinal=ordinal,
                    function_call=function_call,
                    call_id=function_call.call_id,
                )
            )
        exchange = _PendingExchange(
            response_messages=tuple(response_messages),
            slots=tuple(slots),
            result_carrier_item_id=result_carrier_item_id,
        )
        self._pending_exchange = exchange
        return tuple(_ResultCommit(self, exchange, slot) for slot in exchange.slots)

    def seal_exchange(self, result_message: Message) -> None:
        """Replace provisional results with their canonical carrier message."""
        exchange = self._pending_exchange
        if exchange is None:
            return
        result_identities = {id(content) for content in result_message.contents}
        if any(slot.result is not None and id(slot.result) not in result_identities for slot in exchange.slots):
            return
        self._sealed_exchanges.append(
            _SealedExchange(
                messages=(*exchange.response_messages, result_message),
                answered_count=exchange.answered_count,
            )
        )
        self._pending_exchange = None

    def _fill_slot(
        self,
        exchange: _PendingExchange,
        slot: _PendingExchangeSlot,
        result: Content,
        *,
        fill_kind: str,
        upgrade_raw: bool = False,
    ) -> None:
        if exchange is not self._pending_exchange:
            return
        if slot.result is None:
            slot.result = result
            slot.fill_kind = fill_kind
            exchange.projection_messages = None
            # Interrupted fills mark cancellation before the side-effect
            # boundary was observed: they persist through finalization but are
            # not commits, so a zero-commit retry whose own stream teardown
            # cancelled in-flight tools may still re-dispatch them.
            if fill_kind != "interrupted":
                self._committed_count += 1
            self._kick_result_checkpoint()
            return
        if upgrade_raw and slot.fill_kind == "raw":
            slot.result = result
            slot.fill_kind = fill_kind
            exchange.projection_messages = None
            self._kick_result_checkpoint()

    def _interrupt_slot(
        self,
        exchange: _PendingExchange,
        slot: _PendingExchangeSlot,
        function_call: Content,
        invocation_context: FunctionInvocationContext | None,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        result_metadata = dict(metadata or {})
        result_metadata[TOOL_INTERRUPTED_METADATA_KEY] = True
        if slot.fill_kind == "interrupted" and slot.result is not None:
            # Terminal audit persistence can finish after the early
            # interruption fill, including after the exchange was sealed.
            # The slot retains the canonical result object, so merge the late
            # replay references in place and checkpoint the upgraded history.
            existing = slot.result.additional_properties.get(TOOL_RESULT_METADATA_KEY)
            existing_metadata = dict(existing) if isinstance(existing, Mapping) else {}
            slot.result.additional_properties[TOOL_RESULT_METADATA_KEY] = {
                **existing_metadata,
                **result_metadata,
            }
            if invocation_context is not None:
                timing = _tool_trajectory_timing(function_call, invocation_context)
                slot.result.additional_properties[TRAJECTORY_TIMING_KEY] = timing
            self._kick_result_checkpoint()
            return
        if exchange is not self._pending_exchange:
            return
        if slot.fill_kind == "raw" and slot.result is not None:
            result_metadata[TOOL_POST_PROCESSING_INTERRUPTED_METADATA_KEY] = True
            if invocation_context is not None and invocation_context.result is not None:
                # Middleware completed before the cancellation landed, so
                # ``invocation_context.result`` carries the hooks-processed
                # value (redacted text, extra_context, result_override…).
                # Replace the raw slot with that processed result rather than
                # keeping text the hooks had not yet finalised.
                existing = slot.result.additional_properties.get(TOOL_RESULT_METADATA_KEY)
                if isinstance(existing, Mapping):
                    result_metadata = {**existing, **result_metadata}
                additional = _result_additional_properties(function_call, invocation_context)
                additional[TOOL_RESULT_METADATA_KEY] = result_metadata
                processed = Content.from_function_result(
                    call_id=function_call.call_id,  # type: ignore[arg-type]
                    result=invocation_context.result,
                    additional_properties=additional,
                )
                self._fill_slot(exchange, slot, processed, fill_kind="interrupted", upgrade_raw=True)
            else:
                # ``invocation_context is None``: the tool completed via
                # ``SyncToolCancelledAfterCompletion`` and middleware never ran —
                # raw is already the final, safe value; keep it and only
                # update its metadata.
                existing = slot.result.additional_properties.get(TOOL_RESULT_METADATA_KEY)
                if isinstance(existing, Mapping):
                    result_metadata = {**existing, **result_metadata}
                slot.result.additional_properties[TOOL_RESULT_METADATA_KEY] = result_metadata
                timing = _tool_trajectory_timing(function_call, invocation_context)
                slot.result.additional_properties[TRAJECTORY_TIMING_KEY] = timing
                self._kick_result_checkpoint()
            return
        if slot.result is not None:
            # Final results are immutable; raw and interrupted fills took
            # their dedicated merge paths above.
            return
        result_metadata[TOOL_FAILED_METADATA_KEY] = True
        additional = _result_additional_properties(function_call, invocation_context)
        additional[TOOL_RESULT_METADATA_KEY] = result_metadata
        result = Content.from_function_result(
            call_id=function_call.call_id,  # type: ignore[arg-type]
            result=(
                "Error: Tool execution was interrupted. The operation may have completed; "
                "inspect current state before retrying."
            ),
            additional_properties=additional,
        )
        self._fill_slot(exchange, slot, result, fill_kind="interrupted")

    async def _run_pre_wire_barrier(self) -> bool:
        """Strictly persist committed tool work; True only when the barrier reports it persisted."""
        if self._committed_count == 0 or self._barrier_degraded or self._barrier_unconfigured:
            return False
        callback = self._on_pre_wire_barrier
        if callback is None:
            self._barrier_unconfigured = True
            return False
        for _attempt in range(2):
            try:
                outcome = await callback()
            except Exception:
                outcome = RecoveryPersistOutcome.FAILED
            if outcome is RecoveryPersistOutcome.PERSISTED:
                return True
            if outcome is RecoveryPersistOutcome.UNCONFIGURED:
                self._barrier_unconfigured = True
                return False
        self._barrier_degraded = True
        if not self._barrier_warned:
            self._barrier_warned = True
            logger.error(
                "Recovery sidecar persistence failed twice after committed tool work; "
                "continuing with the in-memory journal."
            )
        return False

    def _kick_result_checkpoint(self) -> None:
        callback = self._on_result_checkpoint
        if callback is None:
            return
        task = asyncio.get_running_loop().create_task(callback())
        task.add_done_callback(self._observe_checkpoint_task)

    @staticmethod
    def _observe_checkpoint_task(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            exception = task.exception()
        except Exception:
            logger.debug("LoopRecorder result checkpoint failed", exc_info=True)
            return
        if exception is not None:
            logger.debug(
                "LoopRecorder result checkpoint failed",
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    @staticmethod
    def _project_pending_exchange(exchange: _PendingExchange) -> list[Message]:
        if exchange.projection_messages is not None:
            return list(exchange.projection_messages)
        answered_calls = {id(slot.function_call) for slot in exchange.slots if slot.result is not None}
        staged_calls = {id(slot.function_call) for slot in exchange.slots}
        projected: list[Message] = []
        for message in exchange.response_messages:
            message_staged_calls = [content for content in message.contents if id(content) in staged_calls]
            if not message_staged_calls:
                projected.append(message)
                continue
            answered_in_message = [content for content in message_staged_calls if id(content) in answered_calls]
            informational = any(
                content.type == "function_call" and content.informational_only for content in message.contents
            )
            if not answered_in_message and not informational:
                continue
            contents = [
                content
                for content in message.contents
                if id(content) not in staged_calls or id(content) in answered_calls
            ]
            projected.append(_message_snapshot(message, contents))
        results = [slot.result for slot in exchange.slots if slot.result is not None]
        if results:
            carrier = Message(role="tool", contents=results)
            carrier.additional_properties[ANALYTICS_ITEM_ID_KEY] = exchange.result_carrier_item_id
            projected.append(carrier)
        exchange.projection_messages = tuple(projected)
        return projected

    @staticmethod
    def _dedupe_projected_messages(messages: Sequence[Message]) -> list[Message]:
        seen_contents: set[int] = set()
        projected: list[Message] = []
        for message in messages:
            contents = [content for content in message.contents if id(content) not in seen_contents]
            if not contents:
                continue
            seen_contents.update(id(content) for content in contents)
            projected.append(
                message if len(contents) == len(message.contents) else _message_snapshot(message, contents)
            )
        return projected

    def _checkpoint_messages(self) -> list[Message]:
        """Return the effective message prefix used for checkpoint suppression."""
        if self._capture_service_loop_messages and self._service_loop_messages:
            return self._service_loop_messages
        return self._captured or []

    def _should_suppress_checkpoint(self) -> bool:
        """Return True when the captured prefix has not changed since the last checkpoint."""
        messages = self._checkpoint_messages()
        key = (len(messages), self._hash_last_message(messages))
        if key == self._last_checkpoint_key:
            return True
        self._last_checkpoint_key = key
        return False

    def _hash_last_message(self, messages: list[Message]) -> str:
        if not messages:
            return ""
        try:
            payload = self._message_hasher(messages[-1])
        except Exception:
            payload = repr(messages[-1])
        # Hashing is identity-bearing: surrogatepass keeps the operation total
        # without aliasing a lone surrogate to the literal escape that spells it.
        return hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()
