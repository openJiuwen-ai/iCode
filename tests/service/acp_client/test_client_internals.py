# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Focused tests for ACP ordering, tracking, backpressure, and bounded state."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path

import pytest
from acp.connection import StreamDirection, StreamEvent
from acp.exceptions import RequestError
from acp.schema import AgentMessageChunk, ImageContentBlock, SessionNotification, ToolCallProgress
from acp.task import TaskSupervisor

from chrys.foundation.models.ask_user import ASK_USER_META_COLLISION_KEYS
from chrys.foundation.text.images import MAX_IMAGE_BYTES
from chrys.service.acp_client import AcpTransportError
from chrys.service.acp_client import _transport as transport_mod
from chrys.service.acp_client import client as client_mod
from chrys.service.acp_client import protocol as protocol_mod
from chrys.service.acp_client.spawn import ACP_STDIO_LIMIT_BYTES
from tests.support.waiting import wait_for

_RETAINED_ASK_USER_META_COLLISION_KEYS = frozenset(
    {
        "answers",
        "cancelled",
        "callerName",
        "kwargs",
        "method",
        "params",
        "questions",
        "requestId",
        "sessionId",
        "session_id",
        "toolCall",
        "tool_call",
    }
)


def _observer(
    *,
    store: client_mod._TrackingStateStore,
    updates: client_mod._ObserverBuffer,
    failures: list[BaseException],
    activities: list[None],
) -> client_mod._FrameObserver:
    return client_mod._FrameObserver(
        store=store,
        updates=updates,
        active_session=lambda: "active",
        fail=failures.append,
        activity=lambda: activities.append(None),
    )


@pytest.mark.parametrize("collision_key", sorted(_RETAINED_ASK_USER_META_COLLISION_KEYS))
async def test_request_input_ext_method_rejects_every_retained_meta_collision(
    tmp_path: Path,
    collision_key: str,
) -> None:
    from .helpers import CallbackRecorder, make_spec

    assert ASK_USER_META_COLLISION_KEYS == _RETAINED_ASK_USER_META_COLLISION_KEYS
    callbacks = CallbackRecorder()
    client = client_mod.AcpAgentClient(make_spec(tmp_path), callbacks)
    client._session_id = "active"
    client._observer = _observer(
        store=client_mod._TrackingStateStore(),
        updates=client_mod._ObserverBuffer(),
        failures=[],
        activities=[],
    )

    with pytest.raises(RequestError) as error:
        await client.ext_method(
            "chrys/request_input",
            {
                "sessionId": "active",
                "requestId": "request-1",
                "questions": [{"question": "Continue?", "options": [{"label": "yes"}, {"label": "no"}]}],
                "_meta": {collision_key: "forged"},
            },
        )

    assert error.value.code == -32602
    assert callbacks.ext_calls == []


async def test_tracking_store_retains_no_incoming_payload_and_tracks_prompt_metadata() -> None:
    store = client_mod._TrackingStateStore()

    incoming = store.begin_incoming("large/request", {"secret": "x" * 100_000})
    prompt_future = store.register_outgoing(7, "session/prompt")
    store.mark_write_committed(7)
    record = store.observe_response(
        7,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 2, "totalTokens": 3},
            },
        },
    )

    assert incoming.params is None
    assert record is not None
    assert record.write_committed
    assert record.usage is not None
    assert record.usage.total_tokens == 3
    store.resolve_outgoing(7, {})
    assert await prompt_future == {}


async def test_prompt_barrier_is_id_exact_and_requires_prewrite_commit() -> None:
    store = client_mod._TrackingStateStore()
    updates = client_mod._ObserverBuffer()
    failures: list[BaseException] = []
    activities: list[None] = []
    observer = _observer(store=store, updates=updates, failures=failures, activities=activities)
    prompt_future = store.register_outgoing(11, "session/prompt")

    # Wire tracking runs at the pump: unknown ids are ignored there, and a
    # response racing ahead of its request's write commit is a violation.
    assert store.observe_response(99, {"jsonrpc": "2.0", "id": 99, "result": None}) is None
    with pytest.raises(ValueError, match="write was committed"):
        store.observe_response(11, {"jsonrpc": "2.0", "id": 11, "result": None})
    # The observer keys off pump-side observation: an untracked response
    # frame must produce neither a barrier nor a failure here.
    observer(StreamEvent(StreamDirection.INCOMING, {"jsonrpc": "2.0", "id": 99, "result": None}))
    observer(StreamEvent(StreamDirection.INCOMING, {"jsonrpc": "2.0", "id": 11, "result": None}))

    assert failures == []
    assert not updates._items
    assert store.record_for(11) is not None
    assert not store.record_for(11).response_observed
    store.reject_outgoing(11, ConnectionError("test cleanup"))
    with pytest.raises(ConnectionError):
        await prompt_future

    committed_store = client_mod._TrackingStateStore()
    committed_updates = client_mod._ObserverBuffer()
    committed = _observer(
        store=committed_store,
        updates=committed_updates,
        failures=[],
        activities=[],
    )
    committed_future = committed_store.register_outgoing(12, "session/prompt")
    committed_store.mark_write_committed(12)
    committed_store.observe_response(12, {"jsonrpc": "2.0", "id": 12, "result": None})
    committed(StreamEvent(StreamDirection.INCOMING, {"jsonrpc": "2.0", "id": 12, "result": None}))

    barrier = await committed_updates.get()
    assert isinstance(barrier, client_mod._PromptBarrier)
    assert barrier.request_id == 12
    committed_store.resolve_outgoing(12, None)
    assert await committed_future is None


def test_inbound_id_reuse_is_legal_after_the_response_write_seam() -> None:
    store = client_mod._TrackingStateStore()
    updates = client_mod._ObserverBuffer()
    failures: list[BaseException] = []
    activities: list[None] = []
    observer = _observer(store=store, updates=updates, failures=failures, activities=activities)
    request = {
        "jsonrpc": "2.0",
        "id": "human-1",
        "method": "session/request_permission",
        "params": {"sessionId": "active"},
    }

    observer(StreamEvent(StreamDirection.INCOMING, request))
    assert observer.human_wait_count == 1
    # A duplicate while the request is still outstanding is a violation.
    observer(StreamEvent(StreamDirection.INCOMING, request))
    assert len(failures) == 1

    observer.before_response_send("human-1")
    assert observer.human_wait_count == 0
    # The id retires at the response-write seam: the peer may legally reuse
    # it the moment it reads the response, before the SDK's outgoing pass.
    assert "human-1" not in observer._inbound
    observer(StreamEvent(StreamDirection.INCOMING, request))
    assert len(failures) == 1
    assert observer.human_wait_count == 1
    # The SDK's late outgoing pass must not retire the fresh registration.
    observer(StreamEvent(StreamDirection.OUTGOING, {"jsonrpc": "2.0", "id": "human-1", "result": None}))
    assert "human-1" in observer._inbound
    assert observer.human_wait_count == 1


def test_human_wait_registration_caps_flags_and_recycles_slots() -> None:
    store = client_mod._TrackingStateStore()
    updates = client_mod._ObserverBuffer()
    observer = _observer(store=store, updates=updates, failures=[], activities=[])
    request = {
        "jsonrpc": "2.0",
        "method": "session/request_permission",
        "params": {"sessionId": "active"},
    }
    for index in range(10):
        observer(StreamEvent(StreamDirection.INCOMING, {**request, "id": f"human-{index}"}))

    assert observer.human_wait_count == transport_mod._MAX_HUMAN_WAITS
    assert observer.human_wait_admitted("human-0")
    assert observer.human_wait_admitted("human-7")
    assert not observer.human_wait_admitted("human-8")
    assert not observer.human_wait_admitted("human-9")
    assert not observer.human_wait_admitted(None)

    observer.before_response_send("human-3")
    assert observer.human_wait_count == transport_mod._MAX_HUMAN_WAITS - 1
    observer(StreamEvent(StreamDirection.INCOMING, {**request, "id": "human-10"}))
    assert observer.human_wait_count == transport_mod._MAX_HUMAN_WAITS
    assert observer.human_wait_admitted("human-10")
    assert not observer.human_wait_admitted("human-8")

    observer.before_response_send("human-0")
    assert observer.human_wait_count == transport_mod._MAX_HUMAN_WAITS - 1
    observer.clear()
    assert observer.human_wait_count == 0


def test_foreign_and_unknown_response_frames_do_not_refresh_activity() -> None:
    store = client_mod._TrackingStateStore()
    updates = client_mod._ObserverBuffer()
    failures: list[BaseException] = []
    activities: list[None] = []
    observer = _observer(store=store, updates=updates, failures=failures, activities=activities)

    observer(StreamEvent(StreamDirection.INCOMING, {"jsonrpc": "2.0", "id": 404, "result": None}))
    observer(
        StreamEvent(
            StreamDirection.INCOMING,
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "foreign",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "foreign"},
                    },
                },
            },
        )
    )
    observer(
        StreamEvent(
            StreamDirection.INCOMING,
            {
                "jsonrpc": "2.0",
                "id": "foreign-human",
                "method": "session/request_permission",
                "params": {"sessionId": "foreign"},
            },
        )
    )
    observer.before_response_send("foreign-human")

    assert activities == []
    assert failures == []
    assert observer.human_wait_count == 0


def test_update_buffer_overflow_is_deterministic_and_barrier_lane_is_reserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_mod, "_MAX_UPDATE_ITEMS", 1)
    monkeypatch.setattr(transport_mod, "_MAX_UPDATE_BYTES", 128)
    updates = client_mod._ObserverBuffer()

    updates.put_update({"sessionId": "active"})
    with pytest.raises(AcpTransportError, match="budget"):
        updates.put_update({"sessionId": "active"})
    updates.put_barrier(17)

    assert len(updates._items) == 2
    assert isinstance(updates._items[-1][0], client_mod._PromptBarrier)


async def test_pause_transport_backpressures_attached_forward_reader() -> None:
    reader = asyncio.StreamReader(limit=8)
    pause_transport = client_mod._PauseTransport()
    reader.set_transport(pause_transport)

    reader.feed_data(b"x" * 17)

    assert not pause_transport._reading.is_set()
    assert await reader.read(17) == b"x" * 17
    assert pause_transport._reading.is_set()


async def test_response_send_backstop_clears_human_flag_before_blocked_drain() -> None:
    class _BlockingWriter:
        def __init__(self) -> None:
            self.release = asyncio.Event()

        def write(self, _payload: bytes) -> None:
            return

        async def drain(self) -> None:
            await self.release.wait()

    store = client_mod._TrackingStateStore()
    updates = client_mod._ObserverBuffer()
    observer = _observer(store=store, updates=updates, failures=[], activities=[])
    observer(
        StreamEvent(
            StreamDirection.INCOMING,
            {
                "jsonrpc": "2.0",
                "id": "wait-1",
                "method": "session/request_permission",
                "params": {"sessionId": "active"},
            },
        )
    )
    writer = _BlockingWriter()
    supervisor = TaskSupervisor(source="test")
    sender = client_mod._RetainedSender(
        writer,
        supervisor,
        store=store,
        before_response_send=observer.before_response_send,
        on_failure=lambda _error: None,
    )

    send_task = asyncio.create_task(sender.send({"jsonrpc": "2.0", "id": "wait-1", "result": None}))
    await wait_for(lambda: observer.human_wait_count == 0, description="response-send human backstop")
    # The id is already retired at the write seam, even while drain blocks.
    assert "wait-1" not in observer._inbound
    writer.release.set()
    await send_task
    assert observer._inbound == {}
    await sender.close()
    await supervisor.shutdown()


async def test_sender_death_rejects_current_and_queued_sends() -> None:
    class _FailingWriter:
        def write(self, _payload: bytes) -> None:
            raise BrokenPipeError("closed")

        async def drain(self) -> None:
            return

    store = client_mod._TrackingStateStore()
    first_response = store.register_outgoing(1, "initialize")
    second_response = store.register_outgoing(2, "session/new")
    failures: list[BaseException] = []
    supervisor = TaskSupervisor(source="test")
    sender = client_mod._RetainedSender(
        _FailingWriter(),
        supervisor,
        store=store,
        before_response_send=lambda _request_id: None,
        on_failure=failures.append,
    )

    first_send = asyncio.create_task(sender.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    second_send = asyncio.create_task(sender.send({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}}))
    results = await asyncio.gather(first_send, second_send, return_exceptions=True)
    await wait_for(lambda: len(failures) == 1, description="sender failure hook")

    assert all(isinstance(result, BaseException) for result in results)
    assert store._outgoing == {}
    with pytest.raises(ConnectionError):
        await first_response
    with pytest.raises(ConnectionError):
        await second_response
    with pytest.raises(ConnectionError, match="stopped"):
        await sender.send({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}})
    await sender.emergency_stop()
    await supervisor.shutdown()


async def test_framing_pump_charges_every_frame_before_budget_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_mod, "_MAX_ATTEMPT_FRAMES", 1)
    raw_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    sdk_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    pause_transport = client_mod._PauseTransport()
    sdk_reader.set_transport(pause_transport)
    failures: list[BaseException] = []
    frame = b'{"jsonrpc":"2.0","id":999,"result":null}\n'
    raw_reader.feed_data(frame + frame)
    raw_reader.feed_eof()
    pump = client_mod._FramingPump(
        raw_reader,
        sdk_reader,
        pause_transport,
        stateful=lambda: True,
        closing=lambda: False,
        preflight=lambda message: message,
        on_response=lambda message: None,
        active_session=lambda: None,
        fail=failures.append,
    )

    pump.start()
    task = pump._task
    assert task is not None
    await task

    assert pump.frames == 2
    assert pump.bytes == len(frame) * 2
    assert len(failures) == 1
    assert isinstance(failures[0], AcpTransportError)


async def test_framing_pump_turns_reader_limit_overflow_into_terminal_failure() -> None:
    raw_reader = asyncio.StreamReader(limit=32)
    sdk_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    pause_transport = client_mod._PauseTransport()
    sdk_reader.set_transport(pause_transport)
    failures: list[BaseException] = []
    raw_reader.feed_data(b"x" * 33 + b"\n")
    raw_reader.feed_eof()
    pump = client_mod._FramingPump(
        raw_reader,
        sdk_reader,
        pause_transport,
        stateful=lambda: True,
        closing=lambda: False,
        preflight=lambda message: message,
        on_response=lambda message: None,
        active_session=lambda: None,
        fail=failures.append,
    )

    pump.start()
    task = pump._task
    assert task is not None
    await task

    assert len(failures) == 1
    assert isinstance(failures[0], AcpTransportError)
    assert "oversized" in failures[0].detail


async def test_notification_frames_ride_the_notification_caps() -> None:
    raw_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    sdk_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    pause_transport = client_mod._PauseTransport()
    sdk_reader.set_transport(pause_transport)
    failures: list[BaseException] = []
    small = b'{"jsonrpc":"2.0","method":"_zeta/note","params":{"note":"ok"}}\n'
    long_string = b'{"jsonrpc":"2.0","method":"_zeta/blob","params":{"blob":"' + b"y" * (1024 * 1024 + 64) + b'"}}\n'
    wide = b'{"jsonrpc":"2.0","method":"_zeta/blob","params":{"blob":[' + b"0," * 5000 + b"0]}}\n"
    raw_reader.feed_data(small + long_string + wide)
    raw_reader.feed_eof()
    pump = client_mod._FramingPump(
        raw_reader,
        sdk_reader,
        pause_transport,
        stateful=lambda: True,
        closing=lambda: False,
        preflight=lambda message: message,
        on_response=lambda message: None,
        active_session=lambda: None,
        fail=failures.append,
    )

    pump.start()
    task = pump._task
    assert task is not None
    await task

    # Long strings pass through untouched (only the reader limit bounds them);
    # the one past the item cap is never fed to the SDK side.
    assert await sdk_reader.readline() == small
    assert await sdk_reader.readline() == long_string
    assert await sdk_reader.readline() == b""
    assert len(failures) == 1
    assert isinstance(failures[0], AcpTransportError)
    assert "notification" in failures[0].detail


async def test_maximum_tool_image_update_passes_pump_and_observer_caps() -> None:
    encoded = base64.b64encode(b"x" * MAX_IMAGE_BYTES).decode("ascii")
    params = {
        "sessionId": "active",
        "update": {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "image",
            "title": "Image",
            "content": [
                {
                    "type": "content",
                    "content": {"type": "image", "data": encoded, "mimeType": "image/png"},
                }
            ],
            "status": "completed",
        },
    }
    frame = client_mod.encode_protocol_json({"jsonrpc": "2.0", "method": "session/update", "params": params}) + b"\n"
    assert len(frame) > protocol_mod._MAX_PAYLOAD_BYTES

    raw_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    sdk_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    pause_transport = client_mod._PauseTransport()
    sdk_reader.set_transport(pause_transport)
    failures: list[BaseException] = []
    raw_reader.feed_data(frame)
    raw_reader.feed_eof()
    pump = client_mod._FramingPump(
        raw_reader,
        sdk_reader,
        pause_transport,
        stateful=lambda: True,
        closing=lambda: True,
        preflight=lambda message: message,
        on_response=lambda message: None,
        active_session=lambda: "active",
        fail=failures.append,
    )

    pump.start()
    task = pump._task
    assert task is not None
    await task

    assert failures == []
    assert await sdk_reader.readline() == frame

    updates = client_mod._ObserverBuffer()
    updates.put_update(params)
    assert await updates.get() == params


@pytest.mark.parametrize("text", ["写入文件" * 4096, "😀" * 4096], ids=["cjk", "emoji"])
async def test_update_frames_are_charged_the_bytes_the_agent_sent(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    params = {
        "sessionId": "active",
        "update": {"sessionUpdate": "tool_call_update", "toolCallId": "write-1", "rawOutput": text},
    }
    # Unescaped UTF-8, as an agent may write it; escaped, the same text is far larger.
    message = {"jsonrpc": "2.0", "method": "session/update", "params": params}
    frame = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    assert len(client_mod.encode_protocol_json(params)) > len(frame)

    raw_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    sdk_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    pause_transport = client_mod._PauseTransport()
    sdk_reader.set_transport(pause_transport)
    failures: list[BaseException] = []
    raw_reader.feed_data(frame)
    raw_reader.feed_eof()
    pump = client_mod._FramingPump(
        raw_reader,
        sdk_reader,
        pause_transport,
        stateful=lambda: True,
        closing=lambda: True,
        preflight=lambda message: message,
        on_response=lambda message: None,
        active_session=lambda: "active",
        fail=failures.append,
    )

    pump.start()
    task = pump._task
    assert task is not None
    await task

    assert failures == []
    forwarded = await sdk_reader.readline()
    assert forwarded == frame

    # A frame at the stdio limit fits an empty buffer: the budget is larger,
    # and it charges what the frame spent.
    assert ACP_STDIO_LIMIT_BYTES < transport_mod._MAX_UPDATE_BYTES
    monkeypatch.setattr(transport_mod, "_MAX_UPDATE_BYTES", len(frame))
    updates = client_mod._ObserverBuffer()
    updates.put_update(json.loads(forwarded)["params"])
    assert await updates.get() == params


@pytest.mark.parametrize("spelling", ["toolCallId", "tool_call_id", "messageId", "message_id"])
def test_update_ids_keep_a_length_cap_under_every_spelling_the_sdk_reads(spelling: str) -> None:
    limit = protocol_mod._MAX_UPDATE_ID_CHARS
    # The text beside the id has no cap.
    text = "x" * (protocol_mod._MAX_PAYLOAD_BYTES + 1)
    is_tool_call = spelling in {"toolCallId", "tool_call_id"}

    def update(identifier: str) -> dict[str, object]:
        if is_tool_call:
            body = {"sessionUpdate": "tool_call_update", spelling: identifier, "rawOutput": text}
        else:
            body = {
                "sessionUpdate": "agent_message_chunk",
                spelling: identifier,
                "content": {"type": "text", "text": text},
            }
        return {"sessionId": "sess", "update": body}

    updates = client_mod._ObserverBuffer()
    at_limit = update("i" * limit)
    updates.put_update(at_limit)
    # The spelling reaches the id the translator keeps.
    parsed = SessionNotification.model_validate(at_limit).update
    assert isinstance(parsed, ToolCallProgress if is_tool_call else AgentMessageChunk)
    assert (parsed.tool_call_id if isinstance(parsed, ToolCallProgress) else parsed.message_id) == "i" * limit

    with pytest.raises(AcpTransportError, match="retained-payload caps"):
        updates.put_update(update("i" * (limit + 1)))
    assert len(updates._items) == 1


@pytest.mark.parametrize("tag", ["sessionUpdate", "session_update"])
@pytest.mark.parametrize("mime", ["mimeType", "mime_type"])
def test_tool_images_are_checked_under_every_spelling_the_sdk_reads(tag: str, mime: str) -> None:
    params = {
        "sessionId": "active",
        "update": {
            tag: "tool_call_update",
            "toolCallId": "image",
            "content": [{"type": "content", "content": {"type": "image", "data": "not base64!", mime: "image/png"}}],
        },
    }
    # The SDK takes this spelling as a tool image.
    parsed = SessionNotification.model_validate(params).update
    assert isinstance(parsed, ToolCallProgress)
    assert parsed.content is not None
    assert isinstance(parsed.content[0].content, ImageContentBlock)

    with pytest.raises(AcpTransportError, match="retained-payload caps"):
        client_mod._ObserverBuffer().put_update(params)


def test_tool_image_update_exemption_keeps_decoded_size_cap() -> None:
    encoded = base64.b64encode(b"x" * (MAX_IMAGE_BYTES + 1)).decode("ascii")
    params = {
        "sessionId": "active",
        "update": {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "image",
            "content": [
                {
                    "type": "content",
                    "content": {"type": "image", "data": encoded, "mimeType": "image/png"},
                }
            ],
        },
    }

    with pytest.raises(AcpTransportError, match="retained-payload caps"):
        client_mod._ObserverBuffer().put_update(params)


def test_envelope_rejects_unknown_top_level_members() -> None:
    base = {"jsonrpc": "2.0", "method": "session/update", "params": {}}
    assert client_mod.validate_json_rpc_envelope(dict(base)) == "notification"
    # The per-kind caps run on params/result only; an unknown top-level
    # member would ride around them into the SDK re-parse.
    with pytest.raises(ValueError, match="unknown envelope"):
        client_mod.validate_json_rpc_envelope({**base, "padding": "x"})
    with pytest.raises(ValueError, match="unknown envelope"):
        client_mod.validate_json_rpc_envelope({"jsonrpc": "2.0", "id": 5, "result": {}, "meta": {}})


def test_raw_usage_validation_rejects_coercions_and_bounds() -> None:
    valid = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "usage": {
                "inputTokens": 1,
                "outputTokens": 2,
                "totalTokens": 3,
                "thoughtTokens": 0,
            }
        },
    }

    assert transport_mod._extract_raw_usage(valid) is not None
    for value in (True, 1.0, -1, 1 << 63):
        malformed = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "usage": {
                    "inputTokens": value,
                    "outputTokens": 2,
                    "totalTokens": 3,
                }
            },
        }
        assert transport_mod._extract_raw_usage(malformed) is None


def test_retained_payload_caps_bound_depth_bytes_and_items(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol_mod, "_MAX_PAYLOAD_DEPTH", 1)
    with pytest.raises(ValueError, match="deep"):
        client_mod._validate_payload_caps({"one": {"two": {"three": 3}}})

    monkeypatch.setattr(protocol_mod, "_MAX_PAYLOAD_DEPTH", 32)
    monkeypatch.setattr(protocol_mod, "_MAX_PAYLOAD_BYTES", 8)
    with pytest.raises(ValueError, match="byte"):
        client_mod._validate_payload_caps({"value": "too large"})

    monkeypatch.setattr(protocol_mod, "_MAX_PAYLOAD_BYTES", 1024)
    monkeypatch.setattr(protocol_mod, "_MAX_COLLECTION_ITEMS", 1)
    with pytest.raises(ValueError, match="many"):
        client_mod._validate_payload_caps({"one": 1, "two": 2})

    # Aggregate item bound: every collection is under the per-collection cap,
    # but the nesting multiplies the total item count past it.
    monkeypatch.setattr(protocol_mod, "_MAX_COLLECTION_ITEMS", 6)
    with pytest.raises(ValueError, match="total"):
        client_mod._validate_payload_caps([[1, 2, 3], [4, 5, 6]])

    monkeypatch.setattr(protocol_mod, "_MAX_COLLECTION_ITEMS", 4_096)
    monkeypatch.setattr(protocol_mod, "_MAX_STRING_CHARS", 4)
    with pytest.raises(ValueError, match="string"):
        client_mod._validate_payload_caps({"oversized-key": 1})


def test_agent_messages_take_a_larger_item_total_than_responses() -> None:
    # 500 lists of nine: 5,002 items, each collection under the 4,096 cap.
    wide = {"changes": [[index] * 9 for index in range(500)]}
    protocol_mod._validate_notification_payload_caps(wide)
    protocol_mod._validate_request_payload_caps(wide)
    with pytest.raises(ValueError, match="total"):
        protocol_mod._validate_payload_caps(wide)

    flood = {"changes": [[0] * 4_000 for _ in range(17)]}
    for validate in (protocol_mod._validate_notification_payload_caps, protocol_mod._validate_request_payload_caps):
        with pytest.raises(ValueError, match="total"):
            validate(flood)


def test_json_rpc_envelope_caps_retained_id_and_method_fields() -> None:
    base = {"jsonrpc": "2.0", "method": "session/update", "params": {}}

    assert client_mod.validate_json_rpc_envelope({**base, "id": 7}) == "request"
    with pytest.raises(ValueError, match="retained-field cap"):
        client_mod.validate_json_rpc_envelope({**base, "id": "x" * (protocol_mod._MAX_REQUEST_ID_CHARS + 1)})
    with pytest.raises(ValueError, match="integer range"):
        client_mod.validate_json_rpc_envelope({**base, "id": 1 << 63})
    with pytest.raises(ValueError, match="retained-field cap"):
        client_mod.validate_json_rpc_envelope(
            {"jsonrpc": "2.0", "method": "m" * (protocol_mod._MAX_METHOD_CHARS + 1), "params": {}}
        )
    with pytest.raises(ValueError, match="retained-field cap"):
        client_mod.validate_json_rpc_envelope(
            {"jsonrpc": "2.0", "id": "y" * (protocol_mod._MAX_REQUEST_ID_CHARS + 1), "result": {}}
        )


def test_ask_user_preflight_uses_shared_validation() -> None:
    structured = {
        "sessionId": "active",
        "requestId": "question",
        "questions": [
            {
                "question": "Choose",
                "header": "Choice",
                "options": [{"label": "A", "description": "First"}],
                "multiSelect": False,
            }
        ],
    }
    client_mod._preflight_ask_user(structured)

    with pytest.raises(ValueError, match="parameters"):
        client_mod._preflight_ask_user(
            {
                "sessionId": "active",
                "requestId": "question",
                "question": "Choose",
                "options": ["A"],
            }
        )

    with pytest.raises(ValueError, match="parameters"):
        client_mod._preflight_ask_user({**structured, "question": "Choose"})

    with pytest.raises(ValueError, match="option count"):
        client_mod._preflight_ask_user(
            {
                **structured,
                "questions": [{"question": "Choose", "options": [{"label": f"choice-{index}"} for index in range(9)]}],
            }
        )


async def test_oversized_prompt_response_still_accounts_valid_usage() -> None:
    store = client_mod._TrackingStateStore()
    future = store.register_outgoing(9, "session/prompt")
    store.mark_write_committed(9)
    oversized = {
        "jsonrpc": "2.0",
        "id": 9,
        "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 5, "outputTokens": 3, "totalTokens": 8},
            "blob": "x" * (protocol_mod._MAX_STRING_CHARS + 1),
        },
    }

    with pytest.raises(AcpTransportError, match="retained-payload caps") as observed:
        store.observe_response(9, oversized)

    assert observed.value.usage is not None
    assert observed.value.usage.total_tokens == 8
    record = store.record_for(9)
    assert record is not None
    assert record.usage is not None

    store.resolve_outgoing(9, oversized["result"])
    with pytest.raises(AcpTransportError) as substituted:
        await future
    assert substituted.value.usage is not None
    assert substituted.value.usage.total_tokens == 8


async def test_reap_task_stays_bounded_when_the_task_swallows_cancellation() -> None:
    release = asyncio.Event()

    async def stubborn() -> None:
        while True:
            try:
                await release.wait()
                return
            except asyncio.CancelledError:
                continue

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)

    reaper = asyncio.create_task(client_mod._reap_task(task, timeout_seconds=0.2))
    done, _ = await asyncio.wait({reaper}, timeout=5.0)

    assert reaper in done
    release.set()
    finished, _ = await asyncio.wait({task}, timeout=5.0)
    assert task in finished


async def test_oversized_response_frames_are_rejected_before_retention() -> None:
    store = client_mod._TrackingStateStore()
    store.register_outgoing(3, "initialize")
    store.mark_write_committed(3)
    oversized = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {"agentInfo": {"name": "x" * (protocol_mod._MAX_STRING_CHARS + 1)}},
    }

    with pytest.raises(AcpTransportError, match="retained-payload caps"):
        store.observe_response(3, oversized)

    record = store.record_for(3)
    assert record is not None
    assert not record.response_observed
    assert record.raw_response is None
    assert store.latest_raw_result("initialize") is None

    # The SDK receive loop still routes the same frame into resolve_outgoing
    # after the observer; the flagged record must reject instead of parking
    # the oversized result in the completed record's future.
    store.resolve_outgoing(3, oversized["result"])
    assert store.record_for(3) is record
    assert 3 not in store._outgoing
    with pytest.raises(AcpTransportError, match="retained-payload caps"):
        await record.future

    rejected = store.register_outgoing(5, "session/new")
    store.mark_write_committed(5)
    oversized_error = {
        "jsonrpc": "2.0",
        "id": 5,
        "error": {"code": -32000, "message": "big", "data": "x" * (protocol_mod._MAX_STRING_CHARS + 1)},
    }
    with pytest.raises(AcpTransportError, match="retained-payload caps"):
        store.observe_response(5, oversized_error)
    store.reject_outgoing(5, ConnectionError("sdk error object"))
    with pytest.raises(AcpTransportError, match="retained-payload caps"):
        await rejected


async def test_pump_response_observation_fails_the_attempt_when_caps_are_exceeded(tmp_path) -> None:
    from .helpers import CallbackRecorder, make_spec

    client = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    client._store = client_mod._TrackingStateStore()
    future = client._store.register_outgoing(4, "session/new")
    client._store.mark_write_committed(4)

    with pytest.raises(AcpTransportError, match="retained-payload caps"):
        client._observe_response_at_pump(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "result": {"blob": "x" * (protocol_mod._MAX_STRING_CHARS + 1)},
            }
        )

    record = client._store.record_for(4)
    assert record is not None
    assert record.caps_violated
    assert not record.response_observed
    # Even if SDK-side routing still lands the same frame, the completed
    # record's future must surface the violation, never store the payload.
    client._store.reject_outgoing(4, ConnectionError("sdk error object"))
    with pytest.raises(AcpTransportError, match="retained-payload caps"):
        await future


async def test_pump_announced_session_admits_back_to_back_session_bound_requests(tmp_path) -> None:
    from .helpers import CallbackRecorder, make_spec

    client = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    client._store = client_mod._TrackingStateStore()
    client._store.register_outgoing(2, "session/new")
    request = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "fs/read_text_file",
        "params": {"sessionId": "sess-1"},
    }

    rejected = client._preflight_request(dict(request))
    assert rejected["params"] == {}

    # A response for some other request never announces a session.
    client._sniff_session_response({"jsonrpc": "2.0", "id": 99, "result": {"sessionId": "evil"}})
    assert client._active_session_id() is None

    # A session/new response arriving before its write committed is a protocol
    # violation the observer fails terminally; the sniff must not trust it.
    client._sniff_session_response({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "early"}})
    assert client._active_session_id() is None
    client._store.mark_write_committed(2)

    # The pump sees the session/new response strictly before any frame written
    # after it; announcing at the pump admits the back-to-back request even
    # though open_session has not resumed to assign _session_id yet.
    client._sniff_session_response({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-1"}})
    assert client._active_session_id() == "sess-1"
    admitted = client._preflight_request(dict(request))
    assert admitted["params"] == {"sessionId": "sess-1"}

    client._session_id = "sess-1"
    assert client._active_session_id() == "sess-1"


async def test_duplicate_session_new_response_cannot_announce_after_a_failed_first_response(tmp_path) -> None:
    from .helpers import CallbackRecorder, make_spec

    client = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    client._store = client_mod._TrackingStateStore()
    client._store.register_outgoing(2, "session/new")
    client._store.mark_write_committed(2)

    # The first response claims the id even though it announces nothing.
    client._sniff_session_response({"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "boom"}})
    assert client._active_session_id() is None

    # A duplicate success frame with the same id is ignored, matching the
    # SDK's duplicate-response-id routing.
    client._sniff_session_response({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-dup"}})
    assert client._active_session_id() is None


async def test_session_announcement_requires_a_capped_result_and_bounded_id(tmp_path) -> None:
    from .helpers import CallbackRecorder, make_spec

    # An uncapped result claims the response but must never announce: the id
    # is retained and admits session-bound requests before the store's caps
    # run SDK-side.
    client = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    client._store = client_mod._TrackingStateStore()
    client._store.register_outgoing(2, "session/new")
    client._store.mark_write_committed(2)
    oversized_result = {"sessionId": "s", "blob": "x" * (protocol_mod._MAX_STRING_CHARS + 1)}
    client._sniff_session_response({"jsonrpc": "2.0", "id": 2, "result": oversized_result})
    assert client._active_session_id() is None

    # A capped result whose session id busts the retained-id bound must not
    # announce either.
    other = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    other._store = client_mod._TrackingStateStore()
    other._store.register_outgoing(2, "session/new")
    other._store.mark_write_committed(2)
    oversized_id = "i" * (protocol_mod._MAX_REQUEST_ID_CHARS + 1)
    other._sniff_session_response({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": oversized_id}})
    assert other._active_session_id() is None

    # A capped result that fails NewSessionResponse schema validation must
    # not announce either: open_session will reject it, so an announcement
    # would admit session-bound requests for a session that never opens.
    malformed = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    malformed._store = client_mod._TrackingStateStore()
    malformed._store.register_outgoing(2, "session/new")
    malformed._store.mark_write_committed(2)
    malformed._sniff_session_response(
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "claimed", "modes": {"currentModeId": 7}}}
    )
    assert malformed._active_session_id() is None


def test_update_payloads_ride_the_notification_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    updates = client_mod._ObserverBuffer()
    # A sub-agent streaming a large file: one string past every response cap.
    long_text = {
        "sessionId": "sess",
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "x" * (protocol_mod._MAX_PAYLOAD_BYTES + 1)},
        },
    }

    updates.put_update(long_text)

    assert [params for params, _size in updates._items] == [long_text]
    monkeypatch.setattr(protocol_mod, "_MAX_COLLECTION_ITEMS", 4)
    with pytest.raises(AcpTransportError, match="retained-payload caps"):
        updates.put_update({"sessionId": "sess", "update": {"sessionUpdate": "plan", "entries": [1, 2, 3, 4, 5]}})
    assert len(updates._items) == 1


def test_agent_requests_are_bounded_by_bytes_not_by_string_length(tmp_path: Path) -> None:
    from .helpers import CallbackRecorder, make_spec

    client = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    client._session_id = "sess-1"

    def permission_request(content: str) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-1",
                "toolCall": {"toolCallId": "write-1", "title": "Write", "rawInput": {"content": content}},
                "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}],
            },
        }

    # A file the agent asks to write: past every response cap, within the request cap.
    large = "x" * (protocol_mod._MAX_PAYLOAD_BYTES + 1)
    admitted = permission_request(large)
    assert client._preflight_request(dict(admitted)) == admitted
    client_mod._validate_ext_params("_zeta/write", {"sessionId": "sess-1", "content": large}, "sess-1")

    # Measured as the agent sent it: a two-byte script is three times larger
    # escaped, and still fits a frame once re-encoded for the SDK reader.
    accented = "é" * (protocol_mod._MAX_REQUEST_PAYLOAD_BYTES // 2 - 1024)
    admitted = permission_request(accented)
    assert client._preflight_request(dict(admitted)) == admitted
    assert len(client_mod.encode_protocol_json(admitted)) > protocol_mod._MAX_REQUEST_PAYLOAD_BYTES
    assert len(client_mod.encode_protocol_json(admitted)) < ACP_STDIO_LIMIT_BYTES

    oversized = "x" * protocol_mod._MAX_REQUEST_PAYLOAD_BYTES
    assert client._preflight_request(permission_request(oversized))["params"] == {}
    with pytest.raises(RequestError):
        client_mod._validate_ext_params("_zeta/write", {"sessionId": "sess-1", "content": oversized}, "sess-1")


async def test_deterministic_connect_failures_survive_a_concurrent_eof_terminal(tmp_path) -> None:
    from .helpers import CallbackRecorder, make_spec

    client = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    client._terminal_failure = asyncio.get_running_loop().create_future()
    client._terminal_failure.set_result(client_mod.AcpConnectError("agent stdout closed"))

    config = client_mod.AcpConfigError("The ACP agent returned an unsupported protocol version.")
    classified, root = client._classified_connect_failure(config)
    assert classified is config
    assert root is config

    # Non-deterministic local failures still defer to the terminal root cause.
    classified, root = client._classified_connect_failure(RuntimeError("sdk future rejected"))
    assert isinstance(classified, client_mod.AcpConnectError)
    assert isinstance(root, client_mod.AcpConnectError)


async def test_sender_begin_close_blocks_new_requests_but_lets_responses_drain() -> None:
    written: list[bytes] = []

    class _Writer:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return

    store = client_mod._TrackingStateStore()
    supervisor = TaskSupervisor(source="test")
    sender = client_mod._RetainedSender(
        _Writer(),
        supervisor,
        store=store,
        before_response_send=lambda _request_id: None,
        on_failure=lambda _error: None,
    )
    try:
        sender.begin_close()
        # §14: a draining runner's response must still go out after close
        # admission ends — only NEW requests are refused.
        await sender.send({"jsonrpc": "2.0", "id": "human-1", "result": {"outcome": {"outcome": "cancelled"}}})
        assert written and b"human-1" in written[0]
        with pytest.raises(ConnectionError, match="closing"):
            await sender.send({"jsonrpc": "2.0", "id": 9, "method": "session/prompt", "params": {}})
    finally:
        await sender.close()
        await supervisor.shutdown()


async def test_emergency_stop_stays_bounded_when_the_sender_swallows_cancellation() -> None:
    release = asyncio.Event()
    wrote: list[bytes] = []

    class _Writer:
        def write(self, data: bytes) -> None:
            wrote.append(data)

        async def drain(self) -> None:
            # A misbehaving transport can eat every cancellation the close
            # ladder delivers — including the reap's backstop cancel; only
            # the test teardown frees it.
            while not release.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await release.wait()

    store = client_mod._TrackingStateStore()
    supervisor = TaskSupervisor(source="test")
    sender = client_mod._RetainedSender(
        _Writer(),
        supervisor,
        store=store,
        before_response_send=lambda _request_id: None,
        on_failure=lambda _error: None,
    )
    inflight = asyncio.create_task(
        sender.send({"jsonrpc": "2.0", "id": "human-1", "result": {"outcome": {"outcome": "cancelled"}}})
    )
    await wait_for(lambda: bool(wrote), description="in-flight send reaching drain")
    queued = asyncio.create_task(
        sender.send({"jsonrpc": "2.0", "id": "human-2", "result": {"outcome": {"outcome": "cancelled"}}})
    )
    await wait_for(lambda: sender._queue.qsize() == 1, description="queued send")

    async with asyncio.timeout(30):
        await sender.emergency_stop()

    # The bounded reap returned while the task was still wedged; the queued
    # send AND the already-popped in-flight send are both released without
    # waiting on it — the in-flight future is unreachable from the queue.
    assert not sender._task.done()
    with pytest.raises(ConnectionError, match="stopped"):
        await queued
    async with asyncio.timeout(5):
        with pytest.raises(ConnectionError, match="stopped"):
            await inflight
    release.set()
    # Let the wedged drain return and park on queue.get() before shutdown
    # issues its single cancel — the double would swallow one delivered
    # while still inside the drain loop.
    await wait_for(lambda: sender._active is None, description="wedged send completing")
    await supervisor.shutdown()


async def test_prompt_response_seals_update_intake_and_human_admission() -> None:
    store = client_mod._TrackingStateStore()
    updates = client_mod._ObserverBuffer()
    failures: list[BaseException] = []
    observer = _observer(store=store, updates=updates, failures=failures, activities=[])
    prompt_future = store.register_outgoing(21, "session/prompt")
    store.mark_write_committed(21)
    update_frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "active",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "pre"},
            },
        },
    }
    request_frame = {
        "jsonrpc": "2.0",
        "id": "human-pre",
        "method": "session/request_permission",
        "params": {"sessionId": "active"},
    }

    observer(StreamEvent(StreamDirection.INCOMING, update_frame))
    observer(StreamEvent(StreamDirection.INCOMING, request_frame))
    assert observer.human_wait_admitted("human-pre")
    assert not observer.prompt_sealed

    # A non-prompt response must not seal the attempt.
    other_future = store.register_outgoing(22, "session/set_mode")
    store.mark_write_committed(22)
    store.observe_response(22, {"jsonrpc": "2.0", "id": 22, "result": None})
    observer(StreamEvent(StreamDirection.INCOMING, {"jsonrpc": "2.0", "id": 22, "result": None}))
    assert not observer.prompt_sealed

    store.observe_response(21, {"jsonrpc": "2.0", "id": 21, "result": {"stopReason": "end_turn"}})
    observer(
        StreamEvent(
            StreamDirection.INCOMING,
            {"jsonrpc": "2.0", "id": 21, "result": {"stopReason": "end_turn"}},
        )
    )
    assert observer.prompt_sealed
    # Outstanding admissions are released at the seal so late responses and
    # not-yet-started callbacks auto-deny instead of suspending idle expiry.
    assert not observer.human_wait_admitted("human-pre")
    assert observer.human_wait_count == 0

    observer(StreamEvent(StreamDirection.INCOMING, update_frame))
    observer(StreamEvent(StreamDirection.INCOMING, {**request_frame, "id": "human-post"}))
    assert not observer.human_wait_admitted("human-post")

    items = []
    while updates._items:
        items.append(updates._items.popleft()[0])
    # One pre-seal update plus the barrier; the post-seal update never lands.
    assert [type(item) for item in items] == [dict, client_mod._PromptBarrier]
    assert not failures

    store.resolve_outgoing(21, None)
    store.resolve_outgoing(22, None)
    assert await prompt_future is None
    assert await other_future is None


async def test_frame_observer_drops_foreign_updates_at_intake() -> None:
    store = client_mod._TrackingStateStore()
    updates = client_mod._ObserverBuffer()
    failures: list[BaseException] = []
    observer = _observer(store=store, updates=updates, failures=failures, activities=[])

    def _update_frame(session_id: str, update: dict[str, object]) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }

    chunk = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "x"}}
    observer(StreamEvent(StreamDirection.INCOMING, _update_frame("foreign", chunk)))
    # An over-budget foreign frame must be inert too: dropped before it can
    # reach the buffer's cap checks, not converted into an attempt failure.
    observer(StreamEvent(StreamDirection.INCOMING, _update_frame("foreign", {"blob": list(range(5000))})))

    assert failures == []
    assert updates._data_items == 0

    observer(StreamEvent(StreamDirection.INCOMING, _update_frame("active", chunk)))

    assert failures == []
    assert updates._data_items == 1


async def test_a_response_coalesced_with_eof_defers_the_disconnect(tmp_path: Path) -> None:
    """The pump must track a response before classifying an adjacent EOF.

    When stdout already holds the prompt response and EOF, ``readline`` and
    the EOF branch complete without yielding, so the SDK observer has not run
    yet; only pump-side observation keeps the completed prompt from being
    failed as a disconnect.
    """
    from .helpers import CallbackRecorder, make_spec

    client = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    loop = asyncio.get_running_loop()
    client._stateful = True
    client._store = client_mod._TrackingStateStore()
    client._store.register_outgoing(7, "session/prompt")
    client._store.mark_write_committed(7)
    client._terminal_failure = loop.create_future()
    client._completion_future = loop.create_future()
    client._completion_future.add_done_callback(client_mod._consume_future_exception)

    raw_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    sdk_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    pause_transport = client_mod._PauseTransport()
    sdk_reader.set_transport(pause_transport)
    raw_reader.feed_data(b'{"jsonrpc":"2.0","id":7,"result":{"stopReason":"end_turn"}}\n')
    raw_reader.feed_eof()
    pump = client_mod._FramingPump(
        raw_reader,
        sdk_reader,
        pause_transport,
        stateful=lambda: client._stateful,
        closing=lambda: client._closing,
        preflight=lambda message: message,
        on_response=client._observe_response_at_pump,
        active_session=client._active_session_id,
        fail=client._fail,
    )

    pump.start()
    task = pump._task
    assert task is not None
    await task

    record = client._store.record_for(7)
    assert record is not None
    assert record.response_observed
    assert client._terminal_failure.done()
    assert isinstance(client._terminal_failure.result(), client_mod._AcpDisconnectError)
    # The disconnect is deferred behind the observed response: the completed
    # prompt must not be failed, and no failure-close may race the drain.
    assert not client._completion_future.done()
    assert client._failure_close_task is None


async def test_pump_response_observation_wraps_tracking_violations(tmp_path: Path) -> None:
    from .helpers import CallbackRecorder, make_spec

    client = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    client._store = client_mod._TrackingStateStore()
    client._store.register_outgoing(11, "session/prompt")

    with pytest.raises(AcpTransportError, match="frame observer failed"):
        client._observe_response_at_pump({"jsonrpc": "2.0", "id": 11, "result": None})


async def test_response_observation_is_first_response_wins() -> None:
    store = client_mod._TrackingStateStore()
    store.register_outgoing(7, "session/prompt")
    store.mark_write_committed(7)
    first = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 4, "totalTokens": 5},
        },
    }
    assert store.observe_response(7, first) is not None

    # A later duplicate is inert: no mutation, and no caps run on it.
    oversized_duplicate = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"blob": "x" * (protocol_mod._MAX_STRING_CHARS + 1)},
    }
    assert store.observe_response(7, oversized_duplicate) is None
    record = store.record_for(7)
    assert record is not None
    assert record.raw_response is first
    assert record.usage is not None
    assert record.usage.total_tokens == 5

    # After a caps violation, a well-formed duplicate must not resurrect the
    # attempt's retained response either.
    violated_store = client_mod._TrackingStateStore()
    violated_store.register_outgoing(9, "session/prompt")
    violated_store.mark_write_committed(9)
    with pytest.raises(AcpTransportError, match="retained-payload caps"):
        violated_store.observe_response(
            9,
            {"jsonrpc": "2.0", "id": 9, "result": {"blob": "x" * (protocol_mod._MAX_STRING_CHARS + 1)}},
        )
    assert violated_store.observe_response(9, {"jsonrpc": "2.0", "id": 9, "result": {"stopReason": "end_turn"}}) is None
    violated = violated_store.record_for(9)
    assert violated is not None
    assert violated.caps_violated
    assert not violated.response_observed
    assert violated.raw_response is None


async def test_a_coalesced_duplicate_response_cannot_overwrite_the_first(tmp_path: Path) -> None:
    """Two coalesced responses for one id both reach the pump before SDK routing.

    The SDK resolves only the first; the retained response and usage must
    match it, not the duplicate the pump observed last.
    """
    from .helpers import CallbackRecorder, make_spec

    client = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    loop = asyncio.get_running_loop()
    client._stateful = True
    client._store = client_mod._TrackingStateStore()
    client._store.register_outgoing(7, "session/prompt")
    client._store.mark_write_committed(7)
    client._terminal_failure = loop.create_future()
    client._completion_future = loop.create_future()
    client._completion_future.add_done_callback(client_mod._consume_future_exception)

    raw_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    sdk_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
    pause_transport = client_mod._PauseTransport()
    sdk_reader.set_transport(pause_transport)
    first = (
        b'{"jsonrpc":"2.0","id":7,"result":{"stopReason":"end_turn",'
        b'"usage":{"inputTokens":1,"outputTokens":4,"totalTokens":5}}}\n'
    )
    duplicate = (
        b'{"jsonrpc":"2.0","id":7,"result":{"stopReason":"cancelled",'
        b'"usage":{"inputTokens":100,"outputTokens":400,"totalTokens":500}}}\n'
    )
    raw_reader.feed_data(first + duplicate)
    raw_reader.feed_eof()
    pump = client_mod._FramingPump(
        raw_reader,
        sdk_reader,
        pause_transport,
        stateful=lambda: client._stateful,
        closing=lambda: client._closing,
        preflight=lambda message: message,
        on_response=client._observe_response_at_pump,
        active_session=client._active_session_id,
        fail=client._fail,
    )

    pump.start()
    task = pump._task
    assert task is not None
    await task

    record = client._store.record_for(7)
    assert record is not None
    assert record.response_observed
    assert record.raw_response is not None
    assert record.raw_response["result"]["stopReason"] == "end_turn"
    assert record.usage is not None
    assert record.usage.total_tokens == 5
    # The deferred-disconnect contract still holds with the duplicate behind.
    assert not client._completion_future.done()
    assert client._failure_close_task is None


async def test_cancelled_queued_requests_are_dropped_before_the_close_flush() -> None:
    """A cancelled caller's request must not flush to the agent during close.

    Cancelling ``send()`` cancels the per-item future but leaves the item
    queued, and normal close appends its sentinel behind existing items — a
    terminal-woken or caller-cancelled prompt would otherwise still execute
    remotely. Responses stay owed to the peer regardless.
    """

    class _GatedWriter:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.frames: list[bytes] = []

        def write(self, payload: bytes) -> None:
            self.frames.append(payload)

        async def drain(self) -> None:
            await self.release.wait()

    store = client_mod._TrackingStateStore()
    writer = _GatedWriter()
    supervisor = TaskSupervisor(source="test")
    sender = client_mod._RetainedSender(
        writer,
        supervisor,
        store=store,
        before_response_send=lambda _request_id: None,
        on_failure=lambda _error: None,
    )

    blocker = asyncio.create_task(sender.send({"jsonrpc": "2.0", "method": "initialized", "params": {}}))
    await wait_for(lambda: len(writer.frames) == 1, description="blocker frame written")
    store.register_outgoing(3, "session/prompt")
    prompt_task = asyncio.create_task(
        sender.send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {}})
    )
    response_task = asyncio.create_task(sender.send({"jsonrpc": "2.0", "id": "human-1", "result": None}))
    await wait_for(lambda: sender._queue.qsize() == 2, description="both frames queued behind the drain")
    prompt_task.cancel()
    response_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await prompt_task
    with contextlib.suppress(asyncio.CancelledError):
        await response_task

    close_task = asyncio.create_task(sender.close())
    writer.release.set()
    await close_task
    await blocker
    await supervisor.shutdown()

    joined = b"".join(writer.frames)
    assert b"session/prompt" not in joined
    assert b'"human-1"' in joined
    # The dropped request was never write-committed.
    assert not store.record_for(3).write_committed


async def test_pump_observed_prompt_response_counts_as_sealed_for_idle_expiry(tmp_path: Path) -> None:
    from .helpers import CallbackRecorder, make_spec

    client = client_mod.AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    client._store = client_mod._TrackingStateStore()
    client._updates = client_mod._ObserverBuffer()
    client._observer = client_mod._FrameObserver(
        store=client._store,
        updates=client._updates,
        active_session=lambda: None,
        fail=lambda _failure: None,
        activity=lambda: None,
    )
    client._store.register_outgoing(7, "session/prompt")
    client._store.mark_write_committed(7)
    assert not client._prompt_response_sealed()

    client._store.observe_response(7, {"jsonrpc": "2.0", "id": 7, "result": {"stopReason": "end_turn"}})

    # The pump marked the record but the SDK observer has not sealed yet;
    # idle-expiry arbitration must already treat the attempt as answered.
    assert not client._observer.prompt_sealed
    assert client._prompt_response_sealed()
