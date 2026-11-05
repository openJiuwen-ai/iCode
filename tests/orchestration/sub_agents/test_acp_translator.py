# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AcpUpdateTranslator: remote-update translation into sub-agent events, tool-call accounting, and result text."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import threading

import pytest
from acp.schema import (
    AgentMessageChunk,
    ContentToolCallContent,
    FileEditToolCallContent,
    ImageContentBlock,
    ResourceContentBlock,
    SessionNotification,
    TerminalToolCallContent,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
)

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
)
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.text.images import MAX_IMAGE_BYTES, compress_image_data
from chrys.foundation.tool_result_metadata import TOOL_FAILED_METADATA_KEY, TOOL_INTERRUPTED_METADATA_KEY
from chrys.orchestration.invoker import acp_protocol as acp_protocol_module
from chrys.orchestration.invoker.acp_protocol import AcpUpdateTranslator, preview_text
from chrys.service.acp_client import client as acp_client_module
from chrys.service.acp_client.errors import AcpTransportError
from tests.orchestration.sub_agents._acp_fakes import session_notification
from tests.support.event_capture import capture_events
from tests.support.images import noisy_image_bytes
from tests.support.waiting import wait_for


async def test_translator_synthesizes_progress_before_start_and_prefixes_attempt() -> None:
    bus = EventBus()
    starts = await capture_events(bus, InvocationToolCallStart)
    results = await capture_events(bus, InvocationToolCallResult)
    translator = AcpUpdateTranslator(
        event_bus=bus,
        session_id="parent",
        agent_name="External",
        invocation_id="inv",
        attempt=2,
        origin=InvocationOrigin("sub_agent", "parent", "inv", None),
    )
    update = ToolCallProgress(
        sessionUpdate="tool_call_update",
        toolCallId="remote-call",
        title="Read",
        kind="read",
        rawInput="README.md",
        rawOutput={"ok": True},
        status="failed",
    )
    await translator.put(1, session_notification(update))
    await translator.put(2, session_notification(update))

    assert starts[0].call_id == "a2:remote-call"
    assert starts[0].tool_kind == "filesystem.read"
    assert starts[0].args == {"input": "README.md"}
    assert len(results) == 1
    assert results[0].metadata == {TOOL_FAILED_METADATA_KEY: True}


async def test_translator_preserves_reused_remote_call_id_occurrences_within_attempt() -> None:
    from acp.schema import ToolCallStart

    bus = EventBus()
    starts = await capture_events(bus, InvocationToolCallStart)
    results = await capture_events(bus, InvocationToolCallResult)
    translator = AcpUpdateTranslator(
        event_bus=bus,
        session_id="parent",
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "parent", "inv", None),
    )

    for seq, result in ((1, "first"), (3, "second")):
        await translator.put(
            seq,
            session_notification(
                ToolCallStart(
                    sessionUpdate="tool_call",
                    toolCallId="reused",
                    title="Read",
                    kind="read",
                    status="in_progress",
                )
            ),
        )
        await translator.put(
            seq + 1,
            session_notification(
                ToolCallProgress(
                    sessionUpdate="tool_call_update",
                    toolCallId="reused",
                    title="Read",
                    kind="read",
                    rawOutput=result,
                    status="completed",
                )
            ),
        )

    assert [event.call_id for event in results] == ["a1:reused", "a1:reused:2"]
    assert [event.result for event in results] == ["first", "second"]
    assert {event.call_id for event in starts} == {"a1:reused", "a1:reused:2"}
    assert translator.completed_count == 2


async def test_translator_occurrence_ids_do_not_collide_with_remote_ids() -> None:
    from acp.schema import ToolCallStart

    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    translator = AcpUpdateTranslator(
        event_bus=bus,
        session_id="parent",
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "parent", "inv", None),
    )

    for seq, remote_id, result in ((1, "reused:2", "literal"), (3, "reused", "first"), (5, "reused", "second")):
        await translator.put(
            seq,
            session_notification(
                ToolCallStart(
                    sessionUpdate="tool_call",
                    toolCallId=remote_id,
                    title="Read",
                    status="in_progress",
                )
            ),
        )
        await translator.put(
            seq + 1,
            session_notification(
                ToolCallProgress(
                    sessionUpdate="tool_call_update",
                    toolCallId=remote_id,
                    title="Read",
                    rawOutput=result,
                    status="completed",
                )
            ),
        )

    assert [event.result for event in results] == ["literal", "first", "second"]
    assert len({event.call_id for event in results}) == 3
    assert translator.completed_count == 3


async def test_translator_forwards_only_explicit_remote_hosted_metadata_without_wrapping() -> None:
    bus = EventBus()
    starts = await capture_events(bus, InvocationToolCallStart)
    results = await capture_events(bus, InvocationToolCallResult)
    translator = AcpUpdateTranslator(
        event_bus=bus,
        session_id="parent",
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "parent", "inv", None),
    )
    await translator.put(
        1,
        session_notification(
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId="remote-search",
                title="Search",
                kind="search",
                rawOutput="found",
                status="completed",
            )
        ),
    )
    await translator.put(
        2,
        session_notification(
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId="remote-hosted",
                title="Provider search",
                kind="search",
                rawOutput="found",
                status="completed",
                _meta={
                    "chrys": {
                        "provider_hosted": True,
                        "hosted_family": "search",
                        "provider": "openai",
                        "provider_item_type": "web_search_call",
                        "provider_call_id": "provider-search",
                        "provider_status": "completed",
                    }
                },
            )
        ),
    )

    assert len(results) == 2
    start_by_id = {event.call_id: event for event in starts}
    assert set(start_by_id) == {"a1:remote-search", "a1:remote-hosted"}
    assert start_by_id["a1:remote-search"].provider_hosted is False
    assert start_by_id["a1:remote-search"].hosted_family == ""
    assert start_by_id["a1:remote-hosted"].provider_hosted is True
    assert start_by_id["a1:remote-hosted"].hosted_family == "search"
    assert start_by_id["a1:remote-hosted"].provider_call_id == "provider-search"
    assert results[1].provider_item_type == "web_search_call"


async def test_translator_bounds_remote_hosted_metadata_strings() -> None:
    bus = EventBus()
    starts = await capture_events(bus, InvocationToolCallStart)
    results = await capture_events(bus, InvocationToolCallResult)
    translator = AcpUpdateTranslator(
        event_bus=bus,
        session_id="parent",
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "parent", "inv", None),
    )
    # The client caps no string in an update but its ids.
    long_value = "p" * 64 * 1024
    await translator.put(
        1,
        session_notification(
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId="remote-hosted",
                title="Provider search",
                kind="search",
                rawOutput="found",
                status="completed",
                _meta={
                    "chrys": {
                        "provider_hosted": True,
                        "hosted_family": long_value,
                        "provider": long_value,
                        "provider_item_type": long_value,
                        "provider_call_id": long_value,
                        "provider_status": long_value,
                    }
                },
            )
        ),
    )

    bounded = preview_text(long_value)
    assert len(bounded) < len(long_value)
    assert starts
    assert len(results) == 1
    for event in (*starts, *results):
        assert event.provider_hosted is True
        assert [
            event.hosted_family,
            event.provider,
            event.provider_item_type,
            event.provider_call_id,
            event.provider_status,
        ] == [bounded] * 5


async def test_translator_extracts_every_tool_content_variant_and_flushes_interrupted() -> None:
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    translator = AcpUpdateTranslator(
        event_bus=bus,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )
    await translator.put(
        1,
        session_notification(
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId="content",
                title="Mixed",
                content=[
                    ContentToolCallContent(
                        type="content",
                        content=TextContentBlock(type="text", text="hello"),
                    ),
                    ContentToolCallContent(
                        type="content",
                        content=ResourceContentBlock(type="resource_link", uri="file:///a", name="a"),
                    ),
                    FileEditToolCallContent(type="diff", path="a.py", newText="new"),
                    TerminalToolCallContent(type="terminal", terminalId="term-1"),
                ],
                status="completed",
            )
        ),
    )
    await translator.put(
        2,
        session_notification(
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId="running",
                title="Running",
                status="in_progress",
            )
        ),
    )
    await translator.flush_interrupted()
    assert "hello" in results[0].result
    assert "[resource: file:///a]" in results[0].result
    assert "edited a.py" in results[0].result
    assert "[terminal term-1]" in results[0].result
    assert results[1].result == "(interrupted)"
    assert results[1].metadata == {TOOL_INTERRUPTED_METADATA_KEY: True}
    assert results[1].provider_status == "interrupted"


# No decoder reads this many repeated bytes, so the translator keeps them as they
# came: 4 MiB in base64, past the 5 MiB budget when charged twice.
_LARGE_UNDECODABLE_IMAGE_BYTES = 3 * 1024 * 1024


async def test_translator_accepts_a_large_image_through_the_observer_buffer() -> None:
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    translator = AcpUpdateTranslator(
        event_bus=bus,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )
    encoded = base64.b64encode(b"x" * _LARGE_UNDECODABLE_IMAGE_BYTES).decode("ascii")

    notification = session_notification(
        ToolCallProgress(
            sessionUpdate="tool_call_update",
            toolCallId="image",
            title="Image",
            content=[
                ContentToolCallContent(
                    type="content",
                    content=ImageContentBlock(type="image", data=encoded, mimeType="image/png"),
                )
            ],
            status="completed",
        )
    )
    params = notification.model_dump(mode="json", by_alias=True)
    observer_buffer = acp_client_module._ObserverBuffer()

    observer_buffer.put_update(params)
    buffered = await observer_buffer.get()
    assert isinstance(buffered, dict)
    await translator.put(1, SessionNotification.model_validate(buffered))

    assert len(results) == 1
    assert len(results[0].image_contents) == 1
    assert results[0].image_contents[0].uri.endswith(encoded)


async def test_translator_charges_repeated_image_snapshot_once() -> None:
    translator = AcpUpdateTranslator(
        event_bus=None,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )
    encoded = base64.b64encode(b"x" * _LARGE_UNDECODABLE_IMAGE_BYTES).decode("ascii")

    for seq, status in enumerate(("in_progress", "completed"), start=1):
        await translator.put(
            seq,
            session_notification(
                ToolCallProgress(
                    sessionUpdate="tool_call_update",
                    toolCallId="image",
                    title="Image",
                    content=[
                        ContentToolCallContent(
                            type="content",
                            content=ImageContentBlock(type="image", data=encoded, mimeType="image/png"),
                        )
                    ],
                    status=status,
                )
            ),
        )

    assert translator.completed_count == 1


async def test_translator_replaces_partial_image_charge_with_final_snapshot() -> None:
    translator = AcpUpdateTranslator(
        event_bus=None,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )
    snapshots = (
        (base64.b64encode(b"p" * (1024 * 1024)).decode("ascii"), "in_progress"),
        (base64.b64encode(b"f" * _LARGE_UNDECODABLE_IMAGE_BYTES).decode("ascii"), "completed"),
    )

    for seq, (encoded, status) in enumerate(snapshots, start=1):
        await translator.put(
            seq,
            session_notification(
                ToolCallProgress(
                    sessionUpdate="tool_call_update",
                    toolCallId="image",
                    title="Image",
                    content=[
                        ContentToolCallContent(
                            type="content",
                            content=ImageContentBlock(type="image", data=encoded, mimeType="image/png"),
                        )
                    ],
                    status=status,
                )
            ),
        )

    assert translator.completed_count == 1


async def test_translator_charges_images_retained_by_separate_calls() -> None:
    translator = AcpUpdateTranslator(
        event_bus=None,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )

    for seq, byte in enumerate((b"a", b"b"), start=1):
        encoded = base64.b64encode(byte * _LARGE_UNDECODABLE_IMAGE_BYTES).decode("ascii")
        notification = session_notification(
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId=f"image-{seq}",
                title="Image",
                content=[
                    ContentToolCallContent(
                        type="content",
                        content=ImageContentBlock(type="image", data=encoded, mimeType="image/png"),
                    )
                ],
                status="in_progress",
            )
        )
        if seq == 1:
            await translator.put(seq, notification)
        else:
            with pytest.raises(AcpTransportError, match="translated update budget"):
                await translator.put(seq, notification)


def _image_update(call_id: str, data: bytes) -> SessionNotification:
    return session_notification(
        ToolCallProgress(
            sessionUpdate="tool_call_update",
            toolCallId=call_id,
            title="Image",
            content=[
                ContentToolCallContent(
                    type="content",
                    content=ImageContentBlock(
                        type="image", data=base64.b64encode(data).decode("ascii"), mimeType="image/png"
                    ),
                )
            ],
            status="completed",
        )
    )


def _image_translator(bus: EventBus) -> AcpUpdateTranslator:
    return AcpUpdateTranslator(
        event_bus=bus,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )


async def test_translator_keeps_an_image_over_the_send_limit_compressed() -> None:
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    source = noisy_image_bytes("PNG")
    assert len(source) > MAX_IMAGE_BYTES

    await _image_translator(bus).put(1, _image_update("image", source))

    (image,) = results[0].image_contents
    assert image.media_type == "image/jpeg"
    data = base64.b64decode((image.uri or "").split(",", 1)[1])
    assert data.startswith(b"\xff\xd8\xff")
    assert len(data) <= MAX_IMAGE_BYTES


async def test_translator_keeps_an_image_it_cannot_decode_as_it_came() -> None:
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    source = b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES

    await _image_translator(bus).put(1, _image_update("image", source))

    (image,) = results[0].image_contents
    assert image.media_type == "image/png"
    assert base64.b64decode((image.uri or "").split(",", 1)[1]) == source


async def test_translator_compresses_images_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    loop_thread = threading.get_ident()
    compressing_threads: list[int] = []

    def recording_compress(data: bytes) -> bytes:
        compressing_threads.append(threading.get_ident())
        return compress_image_data(data)

    monkeypatch.setattr(acp_protocol_module, "compress_image_data", recording_compress)
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)

    await _image_translator(bus).put(1, _image_update("image", noisy_image_bytes("PNG")))

    assert len(compressing_threads) == 1
    assert compressing_threads[0] != loop_thread
    assert results[0].image_contents[0].media_type == "image/jpeg"


def _wide_patch(call_id: str, status: str) -> SessionNotification:
    """A tool update editing as many files as a record keeps, each diff as long as it keeps."""
    return session_notification(
        ToolCallProgress(
            sessionUpdate="tool_call_update",
            toolCallId=call_id,
            title="Apply patch",
            kind="edit",
            content=[
                FileEditToolCallContent(type="diff", path=f"/w/m{index}.py", oldText="a" * 2_000, newText="b" * 2_000)
                for index in range(256)
            ],
            status=status,
        )
    )


async def test_translator_charges_a_call_for_its_latest_update_only() -> None:
    """A record keeps only its call's latest state, so a wide patch reported
    again and again stays within the budget that separate calls still fill."""
    translator = AcpUpdateTranslator(
        event_bus=None,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )

    for seq in range(1, 9):
        await translator.put(seq, _wide_patch("patch", "completed" if seq == 8 else "in_progress"))
    assert translator.completed_count == 1

    with pytest.raises(AcpTransportError, match="translated update budget"):
        for seq in range(9, 17):
            await translator.put(seq, _wide_patch(f"patch-{seq}", "in_progress"))


async def test_translator_keeps_no_more_of_a_call_than_its_latest_update() -> None:
    """A finished call whose input a later update replaces with a smaller one
    keeps no copy of the larger input, so what the budget charges bounds what
    its record holds."""
    translator = AcpUpdateTranslator(
        event_bus=None,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )
    wide_input = {f"field{index}": "a" * 2_000 for index in range(256)}

    for seq, call_id in ((1, "one"), (3, "two")):
        await translator.put(
            seq,
            session_notification(
                ToolCallStart(
                    sessionUpdate="tool_call",
                    toolCallId=call_id,
                    title="Write",
                    rawInput=wide_input,
                    status="completed",
                )
            ),
        )
        await translator.put(
            seq + 1,
            session_notification(
                ToolCallProgress(sessionUpdate="tool_call_update", toolCallId=call_id, rawInput={}, status="completed")
            ),
        )

    assert translator.completed_count == 2
    for record in translator.calls.values():
        assert len(json.dumps(dataclasses.astuple(record), default=str)) < 1_024


async def test_translator_message_boundaries_result_modes_and_usage_gauge() -> None:
    bus = EventBus()
    progress = await capture_events(bus, InvocationProgress)
    translator = AcpUpdateTranslator(
        event_bus=bus,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        result_mode="transcript",
        spend_before=21,
        unreported_before=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )
    for seq, (message_id, text) in enumerate((("a", "first"), ("a", " chunk"), ("b", "second")), start=1):
        await translator.put(
            seq,
            session_notification(
                AgentMessageChunk(
                    sessionUpdate="agent_message_chunk",
                    messageId=message_id,
                    content=TextContentBlock(type="text", text=text),
                )
            ),
        )
    await translator.put(
        4,
        session_notification(UsageUpdate(sessionUpdate="usage_update", used=120, size=1_000)),
    )
    assert translator.result_text() == "first chunk\n\nsecond"
    assert translator.unpublished_final_text() == "second"
    assert progress[-1].total_tokens == 120
    assert progress[-1].total_usage_tokens == 21
    assert progress[-1].usage_unreported_attempts == 1
    assert translator.latest_context_size == 1_000


async def test_terminal_publish_is_single_delivery_under_concurrent_flush() -> None:
    """The live consumer completing a call and a cancel-flush must not both
    terminalize it. The terminal claim happens before any await, so whichever
    task wins the guard delivers exactly one InvocationToolCallResult and the
    other returns at the guard — no duplicate result, no double count."""
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    translator = AcpUpdateTranslator(
        event_bus=bus,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )
    await translator.put(
        1,
        session_notification(
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId="dup",
                title="Dup",
                status="in_progress",
            )
        ),
    )

    async def slow_dup_start(event: InvocationToolCallStart) -> None:
        # Yield inside a start publish so a second terminalizer can reach the
        # guard while the first is mid-publish — the exact race window.
        if event.call_id.endswith("dup"):
            await asyncio.sleep(0)

    await bus.subscribe(InvocationToolCallStart, slow_dup_start)

    # The consumer processing a completed update and the cancel-path flush race
    # to terminalize the same record. The update renames the call, so it first
    # suspends publishing the changed start; the flush then reaches the guard
    # and its own start.
    await asyncio.gather(
        translator.put(
            2,
            session_notification(
                ToolCallProgress(
                    sessionUpdate="tool_call_update",
                    toolCallId="dup",
                    title="Dup done",
                    status="completed",
                )
            ),
        ),
        translator.flush_interrupted(),
    )

    dup_results = [r for r in results if r.call_id.endswith("dup")]
    assert len(dup_results) == 1
    assert translator.completed_count == 1


async def test_progress_updates_do_not_split_streaming_message_segments() -> None:
    """Only ToolCallStart seals the open segment (§7.5); progress merges tool state."""
    from acp.schema import ToolCallStart

    translator = AcpUpdateTranslator(
        event_bus=None,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )

    def _chunk(text: str) -> SessionNotification:
        return session_notification(
            AgentMessageChunk(
                sessionUpdate="agent_message_chunk",
                messageId="a",
                content=TextContentBlock(type="text", text=text),
            )
        )

    await translator.put(1, _chunk("prefix "))
    await translator.put(
        2,
        session_notification(
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId="call",
                title="Read",
                kind="read",
                status="in_progress",
            )
        ),
    )
    await translator.put(3, _chunk("suffix"))
    assert translator.result_text() == "prefix suffix"

    bus = EventBus()
    deltas = await capture_events(bus, InvocationContentDelta)
    finished = await capture_events(bus, InvocationContentFinished)
    sealing = AcpUpdateTranslator(
        event_bus=bus,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )
    await sealing.put(
        1,
        session_notification(
            AgentMessageChunk(
                sessionUpdate="agent_message_chunk",
                messageId="a",
                content=TextContentBlock(type="text", text="before tool"),
            )
        ),
    )
    await sealing.put(
        2,
        session_notification(
            ToolCallStart(
                sessionUpdate="tool_call",
                toolCallId="call",
                title="Read",
                kind="read",
                status="in_progress",
            )
        ),
    )
    await sealing.put(
        3,
        session_notification(
            AgentMessageChunk(
                sessionUpdate="agent_message_chunk",
                messageId="a",
                content=TextContentBlock(type="text", text="after tool"),
            )
        ),
    )
    assert sealing.result_text() == "after tool"
    assert sealing.unpublished_final_text() == "after tool"
    assert [(delta.origin.invocation_id, delta.channel, delta.text) for delta in deltas] == [
        ("inv", "answer", "before tool"),
        ("inv", "answer", "after tool"),
    ]
    # The ToolCallStart ended the first segment; the text after it opened another.
    assert [(event.segment_id, event.status) for event in finished] == [(deltas[0].segment_id, "completed")]
    assert deltas[1].segment_id != deltas[0].segment_id


async def test_transcript_mode_reports_no_unpublished_final_after_terminal_tool() -> None:
    from acp.schema import ToolCallStart

    bus = EventBus()
    deltas = await capture_events(bus, InvocationContentDelta)
    translator = AcpUpdateTranslator(
        event_bus=bus,
        session_id=None,
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        result_mode="transcript",
        origin=InvocationOrigin("sub_agent", "", "inv", None),
    )
    await translator.put(
        1,
        session_notification(
            AgentMessageChunk(
                sessionUpdate="agent_message_chunk",
                messageId="a",
                content=TextContentBlock(type="text", text="already published"),
            )
        ),
    )
    await translator.put(
        2,
        session_notification(
            ToolCallStart(
                sessionUpdate="tool_call",
                toolCallId="call",
                title="Read",
                kind="read",
                status="completed",
            )
        ),
    )

    assert translator.result_text() == "already published"
    assert translator.unpublished_final_text() == ""
    assert [delta.text for delta in deltas] == ["already published"]


async def _start_and_result_log(bus: EventBus) -> list[tuple[str, str, str]]:
    """Log each start and result in order: its kind, its call's name, and the provider status it shows."""
    log: list[tuple[str, str, str]] = []

    async def on_start(event: InvocationToolCallStart) -> None:
        log.append(("start", event.tool_name, event.provider_status))

    async def on_result(event: InvocationToolCallResult) -> None:
        log.append(("result", event.tool_name, event.provider_status))

    await bus.subscribe(InvocationToolCallStart, on_start)
    await bus.subscribe(InvocationToolCallResult, on_result)
    return log


def _call_translator(bus: EventBus) -> AcpUpdateTranslator:
    return AcpUpdateTranslator(
        event_bus=bus,
        session_id="parent",
        agent_name="External",
        invocation_id="inv",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "parent", "inv", None),
    )


def _read_update(*, status: str, title: str = "Read", provider_status: str | None = None) -> SessionNotification:
    meta = None
    if provider_status is not None:
        meta = {"chrys": {"provider_hosted": True, "hosted_family": "search", "provider_status": provider_status}}
    return session_notification(
        ToolCallProgress(
            sessionUpdate="tool_call_update",
            toolCallId="call",
            title=title,
            kind="read",
            rawInput={"path": "a"},
            status=status,
            _meta=meta,
        )
    )


async def test_a_call_starts_again_only_when_what_its_start_shows_changes() -> None:
    bus = EventBus()
    log = await _start_and_result_log(bus)
    translator = _call_translator(bus)

    await translator.put(1, _read_update(status="in_progress"))
    await translator.put(2, _read_update(status="in_progress"))
    await translator.put(3, _read_update(status="in_progress", title="Read a"))
    await translator.put(4, _read_update(status="completed", title="Read a"))

    assert log == [("start", "Read", ""), ("start", "Read a", ""), ("result", "Read a", "")]


async def test_a_hosted_call_starts_again_when_it_turns_out_hosted_and_its_status_rides_the_result() -> None:
    bus = EventBus()
    log = await _start_and_result_log(bus)
    translator = _call_translator(bus)

    await translator.put(1, _read_update(status="in_progress"))
    await translator.put(2, _read_update(status="in_progress", provider_status="searching"))
    await translator.put(3, _read_update(status="in_progress", provider_status="reading"))
    await translator.put(4, _read_update(status="completed", provider_status="completed"))

    assert log == [
        ("start", "Read", ""),
        ("start", "Read", "searching"),
        ("result", "Read", "completed"),
    ]


async def test_a_start_a_cancel_cut_off_is_delivered_again_before_the_interrupted_result() -> None:
    bus = EventBus()
    held = asyncio.Event()

    async def hold_the_first_start(_event: InvocationToolCallStart) -> None:
        if not held.is_set():
            held.set()
            await asyncio.Event().wait()

    await bus.subscribe(InvocationToolCallStart, hold_the_first_start)
    # Subscribed after the holder: it misses the start the cancel cuts off.
    log = await _start_and_result_log(bus)
    translator = _call_translator(bus)

    consumer = asyncio.create_task(translator.put(1, _read_update(status="in_progress")))
    await wait_for(lambda: held.is_set() or consumer.done(), description="the start's delivery")
    assert not consumer.done()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await translator.flush_interrupted()

    assert log == [("start", "Read", ""), ("result", "Read", "interrupted")]
