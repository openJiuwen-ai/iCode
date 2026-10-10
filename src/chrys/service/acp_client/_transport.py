# Copyright (c) the agent-client-protocol authors (Chojan Shang, Frost Ming and contributors)
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from the Agent Client Protocol Python SDK (Apache License 2.0; see NOTICE).

"""ACP transport framing, backpressure, observation, and tracking.

``_TrackingStateStore`` and ``_BackpressureDispatcher`` are derived from
``InMemoryMessageStateStore`` (``acp/task/state.py``) and
``DefaultMessageDispatcher`` (``acp/task/dispatcher.py``) in the Agent Client
Protocol Python SDK (https://github.com/agentclientprotocol/python-sdk),
licensed under the Apache License, Version 2.0, and have been modified.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from acp.connection import StreamDirection, StreamEvent
from acp.task import (
    MessageQueue,
    MessageStateStore,
    NotificationRunner,
    RequestRunner,
    RpcTaskKind,
    TaskSupervisor,
)
from acp.task.state import IncomingMessage

from . import protocol
from .errors import AcpConfigError, AcpTransportError, classify_protocol_frame_error
from .protocol import (
    _validate_notification_payload_caps,
    _validate_payload_caps,
    _validate_update_payload_caps,
    encode_protocol_json,
    parse_protocol_json,
    protocol_json_size,
    validate_json_rpc_envelope,
)
from .spec import AcpPromptUsage

logger = logging.getLogger(__name__)

_MAX_ATTEMPT_FRAMES = 100_000
_MAX_ATTEMPT_BYTES = 256 * 1024 * 1024
_MAX_REQUEST_FRAMES = 8_192
_MAX_UPDATE_ITEMS = 4_096
_MAX_UPDATE_BYTES = 64 * 1024 * 1024
_MAX_HUMAN_WAITS = 8
_MAX_USAGE_VALUE = (1 << 63) - 1
_REQUEST_CONCURRENCY = 32
_CLOSE_STAGE_TIMEOUT_SECONDS = 2.0
_REQUEST_DRAIN_TIMEOUT_SECONDS = 1.0

_PROMPT_METHOD = "session/prompt"
_UPDATE_METHOD = "session/update"
_HUMAN_METHODS = frozenset({protocol._PERMISSION_METHOD, protocol._ASK_USER_METHOD})
_CURRENT_INBOUND_ID: contextvars.ContextVar[str | int | None] = contextvars.ContextVar(
    "chrys_acp_inbound_request_id",
    default=None,
)


@dataclass(slots=True)
class _OutgoingRecord:
    request_id: int
    method: str
    future: asyncio.Future[Any]
    write_committed: bool = False
    response_observed: bool = False
    caps_violated: bool = False
    raw_response: dict[str, Any] | None = None
    usage: AcpPromptUsage | None = None


class _TrackingStateStore(MessageStateStore):
    """SDK state store with outgoing metadata and no inbound retention."""

    def __init__(self) -> None:
        self._outgoing: dict[int, _OutgoingRecord] = {}
        self._completed: dict[int, _OutgoingRecord] = {}
        self._prompt_id: int | None = None
        self._prompt_registered = asyncio.Event()

    @property
    def prompt_id(self) -> int | None:
        return self._prompt_id

    async def wait_for_prompt_id(self) -> int:
        await self._prompt_registered.wait()
        if self._prompt_id is None:
            raise RuntimeError("Prompt registration event had no request id.")
        return self._prompt_id

    def reset_prompt(self) -> None:
        self._prompt_id = None
        self._prompt_registered.clear()

    def register_outgoing(self, request_id: int, method: str) -> asyncio.Future[Any]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_consume_future_exception)
        record = _OutgoingRecord(request_id=request_id, method=method, future=future)
        self._outgoing[request_id] = record
        if method == _PROMPT_METHOD:
            self._prompt_id = request_id
            self._prompt_registered.set()
        return future

    def mark_write_committed(self, request_id: int) -> None:
        record = self._outgoing.get(request_id)
        if record is not None:
            record.write_committed = True

    def observe_response(self, request_id: str | int, message: dict[str, Any]) -> _OutgoingRecord | None:
        if type(request_id) is not int:
            return None
        record = self._outgoing.get(request_id)
        if record is None:
            return None
        if not record.write_committed:
            raise ValueError("ACP response arrived before the request write was committed.")
        if record.response_observed or record.caps_violated:
            # First-response-wins: the pump observes every frame, and two
            # coalesced responses for one id both arrive before SDK routing
            # pops the record. The SDK resolves only the first, so a later
            # duplicate must not overwrite the retained response or usage,
            # re-run the caps, or resurrect a caps-violated record.
            return None
        # Retained record fields ride the response caps, the tightest §7.4
        # caps (requests and notifications carry agent-written text and get
        # looser ones): without this, a handful of oversized responses
        # could park most of the attempt byte budget in ``_completed`` forever.
        # The flag is load-bearing beyond the raise: the SDK receive loop still
        # routes this same frame into resolve/reject afterwards (the observer
        # cannot veto it), and those paths must not store the oversized payload
        # in the completed record's future.
        try:
            _validate_payload_caps(message)
        except ValueError as exc:
            record.caps_violated = True
            # §6.2: validly reported usage must still account even when the
            # response itself is rejected — extract it before raising so every
            # resulting AcpTransportError carries it.
            if record.method == _PROMPT_METHOD:
                record.usage = _extract_raw_usage(message)
            raise _AcpProtocolLimitError(
                "ACP response frame exceeds the retained-payload caps.", cause=exc, usage=record.usage
            ) from exc
        record.response_observed = True
        record.raw_response = message
        if record.method == _PROMPT_METHOD:
            record.usage = _extract_raw_usage(message)
        return record

    def record_for(self, request_id: int) -> _OutgoingRecord | None:
        return self._outgoing.get(request_id) or self._completed.get(request_id)

    def outstanding_record(self, request_id: str | int) -> _OutgoingRecord | None:
        """The in-flight record for a response id, or None once routed.

        The observer keys seal/barrier off this instead of ``record_for`` so
        a duplicate response frame arriving after SDK routing stays inert.
        """
        if type(request_id) is not int:
            return None
        return self._outgoing.get(request_id)

    def latest_raw_result(self, method: str) -> dict[str, Any] | None:
        records = [*self._outgoing.values(), *self._completed.values()]
        for record in reversed(records):
            if record.method != method or record.raw_response is None:
                continue
            result = record.raw_response.get("result")
            return result if type(result) is dict else None
        return None

    def resolve_outgoing(self, request_id: int, result: Any) -> None:
        record = self._outgoing.pop(request_id, None)
        if record is not None:
            self._completed[request_id] = record
            if not record.future.done():
                if record.caps_violated:
                    record.future.set_exception(
                        AcpTransportError("ACP response frame exceeds the retained-payload caps.", usage=record.usage)
                    )
                else:
                    record.future.set_result(result)

    def reject_outgoing(self, request_id: int, error: Any) -> None:
        record = self._outgoing.pop(request_id, None)
        if record is not None:
            self._completed[request_id] = record
            if not record.future.done():
                if record.caps_violated:
                    record.future.set_exception(
                        AcpTransportError("ACP response frame exceeds the retained-payload caps.", usage=record.usage)
                    )
                else:
                    record.future.set_exception(error)

    def reject_all_outgoing(self, error: Any) -> None:
        for request_id, record in tuple(self._outgoing.items()):
            self._completed[request_id] = record
            if not record.future.done():
                record.future.set_exception(error)
        self._outgoing.clear()

    def begin_incoming(self, method: str, params: Any) -> IncomingMessage:
        return IncomingMessage(method=method, params=None)

    def complete_incoming(self, record: IncomingMessage, result: Any) -> None:
        return

    def fail_incoming(self, record: IncomingMessage, error: Any) -> None:
        return


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    """Mark background rejection exceptions retrieved without changing await semantics."""
    if not future.cancelled():
        future.exception()


@dataclass(slots=True)
class _PendingSend:
    payload: dict[str, Any]
    encoded: bytes
    future: asyncio.Future[None]


class _RetainedSender:
    """SDK sender replacement with strict encoding and total pending-send failure."""

    def __init__(
        self,
        writer: asyncio.StreamWriter,
        supervisor: TaskSupervisor,
        *,
        store: _TrackingStateStore,
        before_response_send: Callable[[str | int], None],
        on_failure: Callable[[BaseException], None],
    ) -> None:
        self._writer = writer
        self._store = store
        self._before_response_send = before_response_send
        self._on_failure = on_failure
        self._queue: asyncio.Queue[_PendingSend | None] = asyncio.Queue()
        self._closing = False
        self._active: _PendingSend | None = None
        self._task = supervisor.create(self._run(), name="chrys.acp.sender", on_error=self._task_failed)

    async def send(self, payload: dict[str, Any]) -> None:
        is_response = "method" not in payload and ("result" in payload or "error" in payload)
        if self._task.done():
            raise ConnectionError("ACP sender has stopped.")
        if self._closing and not is_response:
            raise ConnectionError("ACP sender is closing.")
        try:
            encoded = encode_protocol_json(payload) + b"\n"
        except ValueError as exc:
            failure = AcpConfigError("A local ACP payload is not valid JSON.", cause=exc)
            request_id = payload.get("id")
            if "method" in payload and type(request_id) is int:
                self._store.reject_outgoing(request_id, failure)
            self._on_failure(failure)
            raise failure from exc
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(_PendingSend(payload=payload, encoded=encoded, future=future))
        await future

    def begin_close(self) -> None:
        self._closing = True

    async def close(self) -> None:
        if not self._closing:
            self._closing = True
        if self._task.done():
            self._reject_pending(ConnectionError("ACP sender stopped."))
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            return
        await self._queue.put(None)
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def emergency_stop(self) -> None:
        self._closing = True
        self._task.cancel()
        # This runs only after the normal close already wedged, so the sender
        # cannot be trusted to honor that single cancellation; a direct await
        # would hand the close ladder to a task that may never finish, and
        # queued sends must be rejected even when it never does.
        await _reap_task(self._task)
        self._reject_pending(ConnectionError("ACP sender stopped."))
        # The item _run already popped is unreachable from the queue; if the
        # task outlived both reap cancellations, its caller must still be
        # released here (both sides guard with future.done()).
        active = self._active
        if active is not None and not active.future.done():
            active.future.set_exception(ConnectionError("ACP sender stopped."))

    async def _run(self) -> None:
        failure: BaseException | None = None
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                if item.future.cancelled() and "method" in item.payload:
                    # Normal close appends its sentinel behind queued items,
                    # so a request whose caller already gave up (caller
                    # cancellation or a terminal wake) would still flush to
                    # the agent and execute remotely. Responses stay owed to
                    # the peer regardless of local cancellation, so only
                    # method-bearing frames are dropped.
                    continue
                self._active = item
                try:
                    payload = item.payload
                    if "method" in payload and "id" in payload and type(payload["id"]) is int:
                        self._store.mark_write_committed(payload["id"])
                    elif "method" not in payload and "id" in payload:
                        self._before_response_send(payload["id"])
                    self._writer.write(item.encoded)
                    await self._writer.drain()
                except BaseException as exc:
                    failure = exc
                    if not item.future.done():
                        item.future.set_exception(exc)
                    raise
                else:
                    if not item.future.done():
                        item.future.set_result(None)
                    self._active = None
        except asyncio.CancelledError:
            raise
        finally:
            self._reject_pending(failure or ConnectionError("ACP sender stopped."))

    def _reject_pending(self, error: BaseException) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item is not None and not item.future.done():
                item.future.set_exception(error)

    def _task_failed(self, _task: asyncio.Task[Any], error: BaseException) -> None:
        self._store.reject_all_outgoing(ConnectionError("ACP sender failed."))
        self._on_failure(error)


class _BackpressureDispatcher:
    """Request-semaphore dispatcher that sinks every notification without tasks."""

    def __init__(
        self,
        *,
        queue: MessageQueue,
        supervisor: TaskSupervisor,
        store: MessageStateStore,
        request_runner: RequestRunner,
        notification_runner: NotificationRunner,
        on_failure: Callable[[BaseException], None],
    ) -> None:
        self._queue = queue
        self._supervisor = supervisor
        self._store = store
        self._request_runner = request_runner
        self._on_failure = on_failure
        self._semaphore = asyncio.Semaphore(_REQUEST_CONCURRENCY)
        self._task: asyncio.Task[None] | None = None
        self._runners: set[asyncio.Task[Any]] = set()
        self._admitting = True

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("ACP dispatcher already started.")
        self._task = self._supervisor.create(
            self._run(),
            name="chrys.acp.dispatcher",
            on_error=self._dispatcher_failed,
        )

    async def _run(self) -> None:
        async for task in self._queue:
            try:
                if task.kind is not RpcTaskKind.REQUEST or not self._admitting:
                    continue
                await self._semaphore.acquire()
                if not self._admitting:
                    self._semaphore.release()
                    continue
                self._dispatch_request(task.message)
            finally:
                self._queue.task_done()

    def _dispatch_request(self, message: dict[str, Any]) -> None:
        record = self._store.begin_incoming(message["method"], message.get("params"))

        async def runner() -> None:
            token = _CURRENT_INBOUND_ID.set(message["id"])
            try:
                result = await self._request_runner(message)
            except Exception as exc:
                self._store.fail_incoming(record, exc)
                raise
            else:
                self._store.complete_incoming(record, result)
            finally:
                _CURRENT_INBOUND_ID.reset(token)
                self._semaphore.release()

        task = self._supervisor.create(runner(), name="chrys.acp.request")
        self._runners.add(task)
        task.add_done_callback(self._runners.discard)

    def close_admission(self) -> None:
        self._admitting = False
        if self._task is not None:
            self._task.cancel()

    async def drain_runners(self) -> None:
        if not self._runners:
            return
        done, pending = await asyncio.wait(tuple(self._runners), timeout=_REQUEST_DRAIN_TIMEOUT_SECONDS)
        for task in pending:
            task.cancel()
        cancelled_done: set[asyncio.Task[Any]] = set()
        if pending:
            cancelled_done, _still_pending = await asyncio.wait(
                pending,
                timeout=_REQUEST_DRAIN_TIMEOUT_SECONDS,
            )
        for task in (*done, *cancelled_done):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def stop(self) -> None:
        self.close_admission()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def cancel_runners(self) -> None:
        """Cancel in-flight request callbacks without closing admission.

        Admission stays open so requests arriving after the prompt seal are
        still dispatched and politely auto-denied by the callbacks; the close
        ladder remains the sole owner of dispatcher teardown. Supervisor error
        hooks skip cancelled tasks, so this never feeds the failure path.
        """
        for task in tuple(self._runners):
            task.cancel()
        await self.drain_runners()

    async def emergency_stop(self) -> None:
        self.close_admission()
        await self.cancel_runners()

    def _dispatcher_failed(self, _task: asyncio.Task[Any], error: BaseException) -> None:
        self._on_failure(error)


@dataclass(frozen=True, slots=True)
class _PromptBarrier:
    request_id: int


class _AcpDisconnectError(AcpTransportError):
    """EOF after the exact prompt response; drain the barrier before closing."""


class _AcpProtocolLimitError(AcpTransportError):
    """Deterministic cap or budget violation; never retryable pre-stateful."""


class _ObserverBuffer:
    """Bounded update lane plus an unmetered control-sentinel lane."""

    def __init__(self) -> None:
        self._items: deque[tuple[dict[str, Any] | _PromptBarrier, int]] = deque()
        self._data_items = 0
        self._data_bytes = 0
        self._ready = asyncio.Event()
        self._closed = False

    def put_update(self, params: dict[str, Any]) -> None:
        # Updates ride the notification caps (nesting and item count) plus the
        # id and tool-image checks. Their size is bounded by the stdio frame
        # limit and this buffer's byte budget, not per string: a sub-agent
        # writing a large file streams one long string that the translator
        # only previews. The budget charges the bytes the frame spent, so one
        # frame at the stdio limit fits an empty buffer whatever its script.
        try:
            _validate_update_payload_caps(params)
        except ValueError as exc:
            raise AcpTransportError("An ACP update exceeds the retained-payload caps.", cause=exc) from exc
        size = protocol_json_size(params)
        if self._data_items >= _MAX_UPDATE_ITEMS or self._data_bytes + size > _MAX_UPDATE_BYTES:
            raise AcpTransportError("The ACP update buffer exceeded its safety budget.")
        self._items.append((params, size))
        self._data_items += 1
        self._data_bytes += size
        self._ready.set()

    def put_barrier(self, request_id: int) -> None:
        self._items.append((_PromptBarrier(request_id), 0))
        self._ready.set()

    async def get(self) -> dict[str, Any] | _PromptBarrier:
        while not self._items:
            if self._closed:
                raise EOFError
            self._ready.clear()
            await self._ready.wait()
        item, size = self._items.popleft()
        if size:
            self._data_items -= 1
            self._data_bytes -= size
        return item

    def close(self) -> None:
        self._closed = True
        self._ready.set()


@dataclass(slots=True)
class _InboundRequest:
    method: str
    human_wait: bool


class _FrameObserver:
    """Synchronous SDK observer retaining exact wire order without raising."""

    def __init__(
        self,
        *,
        store: _TrackingStateStore,
        updates: _ObserverBuffer,
        active_session: Callable[[], str | None],
        fail: Callable[[BaseException], None],
        activity: Callable[[], None],
    ) -> None:
        self._store = store
        self._updates = updates
        self._active_session = active_session
        self._fail = fail
        self._activity = activity
        self._inbound: dict[str | int, _InboundRequest] = {}
        self._human_waits = 0
        self._sealed = False

    @property
    def human_wait_count(self) -> int:
        return self._human_waits

    @property
    def prompt_sealed(self) -> bool:
        """Whether the exact prompt response has been observed on the wire."""
        return self._sealed

    def human_wait_admitted(self, request_id: str | int | None) -> bool:
        """Return whether this request holds one of the capped human-wait slots."""
        if request_id is None:
            return False
        request = self._inbound.get(request_id)
        return request is not None and request.human_wait

    def __call__(self, event: StreamEvent) -> None:
        try:
            if event.direction is StreamDirection.INCOMING:
                self._observe_incoming(event.message)
            else:
                self._observe_outgoing(event.message)
        except BaseException as exc:
            failure = (
                exc
                if isinstance(exc, AcpTransportError)
                else AcpTransportError(
                    "The ACP frame observer failed.",
                    cause=exc,
                )
            )
            self._fail(failure)

    def _observe_incoming(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            request_id = message["id"]
            if request_id in self._inbound:
                raise ValueError("Duplicate outstanding inbound JSON-RPC id.")
            method = message["method"]
            active_session = self._is_active_session_frame(message)
            # Registration is capped here, at arrival: an uncapped flag from a
            # request the dispatcher never admits would suspend idle expiry
            # forever. Unflagged requests are auto-rejected by the callbacks.
            # A sealed attempt admits no new waits: the turn is already over.
            human_wait = (
                method in _HUMAN_METHODS
                and active_session
                and self._human_waits < _MAX_HUMAN_WAITS
                and not self._sealed
            )
            self._inbound[request_id] = _InboundRequest(method=method, human_wait=human_wait)
            if human_wait:
                self._human_waits += 1
            if active_session:
                self._activity()
            return

        if "method" in message:
            if message["method"] == _UPDATE_METHOD:
                if self._sealed:
                    # Wire order is queue order, so anything observed after
                    # the exact prompt response would land behind the barrier
                    # and leak into the sink after prompt() returned.
                    logger.debug("Dropping an ACP session/update observed after the prompt response")
                    return
                if not self._is_active_session_frame(message):
                    # Foreign traffic is inert by contract: it must not
                    # consume the active attempt's update budget, and a
                    # malformed or oversized foreign frame must not be able
                    # to fail the attempt from here or from the consumer.
                    logger.debug("Dropping an ACP session/update for a foreign session")
                    return
                self._activity()
                self._updates.put_update(message.get("params", {}))
            elif self._is_active_session_frame(message):
                self._activity()
            return

        request_id = message["id"]
        # The pump already ran ``observe_response`` in frame order (a response
        # coalesced with EOF must be tracked before the EOF classification);
        # this branch only orders seal/barrier against the queued updates.
        record = self._store.outstanding_record(request_id)
        if record is None or not record.response_observed:
            return
        self._activity()
        if record.method == _PROMPT_METHOD:
            if record.request_id == self._store.prompt_id:
                self._seal_prompt()
            self._updates.put_barrier(record.request_id)

    def _seal_prompt(self) -> None:
        """Mark the attempt terminal at the exact prompt response.

        Sealing happens synchronously in the pump's frame order, so every
        later frame — updates, new human requests — sees it, and the idle
        watchdog can never blame the agent for local post-response drains.
        Outstanding human-wait slots are released here: their callbacks are
        auto-denied (not yet started) or cancelled (in flight) by prompt()'s
        completion path and must not suspend idle expiry meanwhile.
        """
        self._sealed = True
        for request in self._inbound.values():
            request.human_wait = False
        self._human_waits = 0

    def _observe_outgoing(self, message: dict[str, Any]) -> None:
        # Inbound ids are retired at the response-write seam
        # (before_response_send). This observer runs only after the SDK
        # finishes its send, and the peer may legally reuse an id the moment
        # it reads the response — popping here could retire the reused id's
        # fresh registration.
        return

    def before_response_send(self, request_id: str | int) -> None:
        # The write seam is the id's end of life: writer.write follows this
        # call synchronously, and every response flows through the retained
        # sender, so retirement here is total and the peer may reuse the id
        # as soon as it reads the response.
        request = self._inbound.pop(request_id, None)
        if request is not None and request.human_wait:
            request.human_wait = False
            self._human_waits -= 1
            self._activity()

    def clear_human_wait(self, request_id: str | int | None) -> None:
        if request_id is None:
            return
        request = self._inbound.get(request_id)
        if request is not None and request.human_wait:
            request.human_wait = False
            self._human_waits -= 1
            self._activity()

    def clear(self) -> None:
        self._inbound.clear()
        self._human_waits = 0
        self._activity()

    def _is_active_session_frame(self, message: dict[str, Any]) -> bool:
        return _is_active_session_payload(message, self._active_session())


def _is_active_session_payload(message: dict[str, Any], active: str | None) -> bool:
    if active is None:
        return False
    params = message.get("params")
    return type(params) is dict and type(params.get("sessionId")) is str and params["sessionId"] == active


class _PauseTransport:
    """StreamReader pause/resume hook that backpressures the raw framing pump."""

    def __init__(self) -> None:
        self._reading = asyncio.Event()
        self._reading.set()

    async def wait_until_reading(self) -> None:
        await self._reading.wait()

    def pause_reading(self) -> None:
        self._reading.clear()

    def resume_reading(self) -> None:
        self._reading.set()


class _FramingPump:
    """Validate and meter raw stdout before forwarding frames to the SDK."""

    def __init__(
        self,
        raw_reader: asyncio.StreamReader,
        sdk_reader: asyncio.StreamReader,
        pause_transport: _PauseTransport,
        *,
        stateful: Callable[[], bool],
        closing: Callable[[], bool],
        preflight: Callable[[dict[str, Any]], dict[str, Any]],
        on_response: Callable[[dict[str, Any]], None],
        active_session: Callable[[], str | None],
        fail: Callable[[BaseException], None],
    ) -> None:
        self._raw_reader = raw_reader
        self._sdk_reader = sdk_reader
        self._pause_transport = pause_transport
        self._stateful = stateful
        self._closing = closing
        self._preflight = preflight
        self._on_response = on_response
        self._active_session = active_session
        self._fail = fail
        self._frames = 0
        self._bytes = 0
        self._request_frames = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def frames(self) -> int:
        return self._frames

    @property
    def bytes(self) -> int:
        return self._bytes

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="chrys.acp.framing")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        if task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None
        # The SDK receive loop blocks on this reader; without EOF it would
        # outlive a close whose SDK-side shutdown never runs (wedged sender).
        self._sdk_reader.feed_eof()

    async def _run(self) -> None:
        try:
            while True:
                await self._pause_transport.wait_until_reading()
                try:
                    line = await self._raw_reader.readline()
                except ValueError as exc:
                    raise _AcpProtocolLimitError("The ACP agent emitted an oversized stdout frame.", cause=exc) from exc
                if not line:
                    if not self._closing():
                        failure = (
                            _AcpDisconnectError("The ACP process closed its protocol stream.")
                            if self._stateful()
                            else classify_protocol_frame_error(
                                EOFError("ACP stdout closed."),
                                stateful=False,
                                complete=False,
                            )
                        )
                        self._fail(failure)
                    self._sdk_reader.feed_eof()
                    return
                self._frames += 1
                self._bytes += len(line)
                if self._frames > _MAX_ATTEMPT_FRAMES or self._bytes > _MAX_ATTEMPT_BYTES:
                    raise _AcpProtocolLimitError("The ACP inbound frame budget was exhausted.")

                complete = line.endswith(b"\n")
                raw = line[:-1]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                if not complete:
                    raise classify_protocol_frame_error(
                        EOFError("ACP frame ended without a newline."),
                        stateful=self._stateful(),
                        complete=False,
                    )
                try:
                    message = parse_protocol_json(raw)
                    kind = validate_json_rpc_envelope(message)
                except BaseException as exc:
                    raise classify_protocol_frame_error(
                        exc,
                        stateful=self._stateful(),
                        complete=True,
                    ) from exc
                if kind == "request":
                    self._request_frames += 1
                    if self._request_frames > _MAX_REQUEST_FRAMES:
                        raise _AcpProtocolLimitError("The ACP inbound request-frame budget was exhausted.")
                    message = self._preflight(message)
                    raw = encode_protocol_json(message)
                elif kind == "notification":
                    if message.get("method") == _UPDATE_METHOD and not _is_active_session_payload(
                        message, self._active_session()
                    ):
                        # Foreign traffic is inert by contract even when
                        # oversized: it must be dropped before the caps can
                        # turn it transport-fatal. A flood still burns the
                        # attempt frame budget above, so it stays bounded.
                        logger.debug("Dropping an ACP session/update for a foreign session at the pump")
                        continue
                    # The SDK re-parses every forwarded notification and the
                    # dispatcher caps only session/update, so ext/unknown
                    # methods must ride the notification caps here.
                    # Notifications owe no response, so a violation is
                    # transport-fatal (matching put_update) rather than
                    # substituted like request params.
                    try:
                        params = message.get("params")
                        if message.get("method") == _UPDATE_METHOD:
                            _validate_update_payload_caps(params)
                        else:
                            _validate_notification_payload_caps(params)
                    except ValueError as exc:
                        raise _AcpProtocolLimitError(
                            "An ACP notification exceeds the retained-payload caps.",
                            cause=exc,
                        ) from exc
                elif kind == "response":
                    # The pump is the single serial point ahead of SDK routing;
                    # announcing here is what keeps preflight/observer session
                    # checks race-free for frames written back-to-back with the
                    # session/new response.
                    self._on_response(message)
                self._sdk_reader.feed_data(raw + b"\n")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fail(exc)
            self._sdk_reader.feed_eof()


def _extract_raw_usage(message: dict[str, Any]) -> AcpPromptUsage | None:
    result = message.get("result")
    usage = result.get("usage") if type(result) is dict else None
    if usage is None:
        error = message.get("error")
        data = error.get("data") if type(error) is dict else None
        usage = data.get("usage") if type(data) is dict else None
    if type(usage) is not dict:
        return None
    required = ("inputTokens", "outputTokens", "totalTokens")
    optional = ("cachedReadTokens", "cachedWriteTokens", "thoughtTokens")
    if any(not _valid_usage_int(usage.get(field)) for field in required):
        return None
    if any(field in usage and usage[field] is not None and not _valid_usage_int(usage[field]) for field in optional):
        return None
    return AcpPromptUsage(
        input_tokens=usage["inputTokens"],
        output_tokens=usage["outputTokens"],
        total_tokens=usage["totalTokens"],
        cached_read_tokens=usage.get("cachedReadTokens"),
        cached_write_tokens=usage.get("cachedWriteTokens"),
        thought_tokens=usage.get("thoughtTokens"),
    )


def _valid_usage_int(value: Any) -> bool:
    return type(value) is int and 0 <= value <= _MAX_USAGE_VALUE


async def _reap_task(
    task: asyncio.Task[Any],
    *,
    timeout_seconds: float = _CLOSE_STAGE_TIMEOUT_SECONDS,
) -> None:
    """Bounded task reap that cannot be held hostage by swallowed cancellation.

    ``await task`` would forward an outer cancellation into the awaited task
    (``Task.cancel`` cancels ``_fut_waiter``); a task that swallows that single
    CancelledError would strand this reaper after ``asyncio.timeout`` spent its
    one shot. ``asyncio.wait`` never cancels the watched task and returns at
    the deadline unconditionally, so the close ladder below stays reachable.
    """
    task.add_done_callback(_consume_future_exception)
    pending: set[asyncio.Task[Any]] = {task}
    with contextlib.suppress(asyncio.CancelledError, Exception):
        _, pending = await asyncio.wait(pending, timeout=timeout_seconds)
    for stuck in pending:
        stuck.cancel()
