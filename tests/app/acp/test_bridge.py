# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Chrys event to ACP update projection."""

from __future__ import annotations

import uuid

import pytest
from acp.schema import SessionNotification

from chrys.app.acp.bridge import AcpEventBridge, plan_update_for_todos, tool_call_title
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationAborted,
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    InvocationPaused,
    InvocationProgress,
    InvocationRetryAttempt,
    InvocationStarted,
    InvocationSteered,
    InvocationToolCallArgsUpdated,
    InvocationToolCallProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
    InvocationToolCallStatusUpdated,
    SessionSaved,
    TodoListUpdated,
    UsageUpdate,
)
from chrys.foundation.models.invocations import ContentChannel, InvocationOrigin
from chrys.foundation.models.todos import TodoItem
from chrys.foundation.tool_kinds import (
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_MCP,
    KIND_SHELL,
    KIND_SUB_AGENT,
)
from chrys.foundation.tool_result_metadata import (
    PROCESS_EXIT_CODE_METADATA_KEY,
    SHELL_EXIT_CODE_METADATA_KEY,
    SHELL_TIMED_OUT_METADATA_KEY,
    TOOL_ERRORED_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
)
from chrys.kernel import Content
from chrys.orchestration.invoker.acp_protocol import AcpUpdateTranslator

_TURN = InvocationOrigin("turn", "", "turn-test", None)


def _delta(segment_id: str, channel: ContentChannel, text: str) -> InvocationContentDelta:
    return InvocationContentDelta(origin=_TURN, segment_id=segment_id, channel=channel, text=text)


def _finished(segment_id: str, channel: ContentChannel = "answer") -> InvocationContentFinished:
    return InvocationContentFinished(origin=_TURN, segment_id=segment_id, channel=channel)


def test_bridge_maps_answer_and_reasoning_deltas_to_message_and_thought_chunks() -> None:
    bridge = AcpEventBridge()

    updates = [
        *bridge.updates_for_event(_delta("think", "reasoning", "Checking")),
        *bridge.updates_for_event(_delta("think", "reasoning", " the parser.")),
        *bridge.updates_for_event(_finished("think", "reasoning")),
        *bridge.updates_for_event(_delta("summary", "reasoning_summary", "Parser checked.")),
        *bridge.updates_for_event(_finished("summary", "reasoning_summary")),
        *bridge.updates_for_event(_delta("answer", "answer", "Hel")),
        *bridge.updates_for_event(_delta("answer", "answer", "lo")),
        *bridge.updates_for_event(_finished("answer")),
        # The final message carries no text: its answer already streamed.
        *bridge.updates_for_event(InvocationMessage(text="Hello", is_final=True, origin=_TURN)),
    ]

    assert [(update.session_update, update.content.text) for update in updates] == [
        ("agent_thought_chunk", "Checking"),
        ("agent_thought_chunk", " the parser."),
        ("agent_thought_chunk", "Parser checked."),
        ("agent_message_chunk", "Hel"),
        ("agent_message_chunk", "lo"),
    ]
    assert [update.field_meta["chrys"] for update in updates] == [
        {"segmentId": "think", "channel": "reasoning"},
        {"segmentId": "think", "channel": "reasoning"},
        {"segmentId": "summary", "channel": "reasoning_summary"},
        {"segmentId": "answer", "channel": "answer"},
        {"segmentId": "answer", "channel": "answer"},
    ]
    # One message id per segment, in the UUID form ACP requires.
    ids = [update.message_id for update in updates]
    assert ids[0] == ids[1] and ids[3] == ids[4]
    assert len({ids[0], ids[2], ids[3]}) == 3
    assert all(str(uuid.UUID(message_id)) == message_id for message_id in ids)


def test_bridge_skips_empty_deltas() -> None:
    assert AcpEventBridge().updates_for_event(_delta("answer", "answer", "")) == []


def test_a_retry_after_text_neither_retracts_nor_resends_it() -> None:
    bridge = AcpEventBridge()

    first = bridge.updates_for_event(_delta("partial", "answer", "failed partial"))
    closed = bridge.updates_for_event(InvocationContentFinished(origin=_TURN, segment_id="partial", status="failed"))
    boundary = bridge.updates_for_event(
        InvocationRetryAttempt(message="wire dropped", attempt=1, max_attempts=2, delay_seconds=0, origin=_TURN)
    )
    fresh = bridge.updates_for_event(_delta("fresh", "answer", "fresh answer"))

    assert [update.content.text for update in first + fresh] == ["failed partial", "fresh answer"]
    assert closed == boundary == []
    assert first[0].message_id != fresh[0].message_id


def test_bridge_maps_tool_lifecycle_updates() -> None:
    bridge = AcpEventBridge()

    start = bridge.updates_for_event(
        InvocationToolCallStart(
            tool_name="read_file",
            tool_kind=KIND_FILESYSTEM_READ,
            call_id="c1",
            args={"path": "a.py"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]
    progress = bridge.updates_for_event(
        InvocationToolCallProgress(
            tool_name="bash", call_id="c1", lines=["one", "two"], origin=InvocationOrigin("turn", "", "turn-test", None)
        )
    )[0]
    result = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="bash", call_id="c1", result="ok", origin=InvocationOrigin("turn", "", "turn-test", None)
        )
    )[0]

    assert start.session_update == "tool_call"
    assert start.title == "a.py"
    assert start.kind == "read"
    assert start.status == "in_progress"
    assert start.raw_input == {"path": "a.py"}
    assert progress.session_update == "tool_call_update"
    assert progress.status == "in_progress"
    assert progress.raw_output == "one\ntwo"
    assert result.status == "completed"
    assert result.raw_output == "ok"


def test_bridge_maps_hosted_args_progress_and_structured_status() -> None:
    bridge = AcpEventBridge()

    args = bridge.updates_for_event(
        InvocationToolCallArgsUpdated(
            call_id="hosted:1",
            args={"query": "Chrys"},
            provider_hosted=True,
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]
    progress = bridge.updates_for_event(
        InvocationToolCallProgress(
            call_id="hosted:1",
            lines=["searching"],
            provider_hosted=True,
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]
    failed = bridge.updates_for_event(
        InvocationToolCallStatusUpdated(
            call_id="hosted:1",
            status="failed",
            provider_status="failed",
            provider_hosted=True,
            metadata={"result_text": "Error: provider failed"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert args.status == "in_progress"
    assert args.raw_input == {"query": "Chrys"}
    assert progress.status == "in_progress"
    assert progress.raw_output == "searching"
    assert failed.status == "failed"
    assert failed.raw_output == "Error: provider failed"


def test_tool_call_title_prefers_summary_then_shell_command() -> None:
    # A model-provided intent summary wins and collapses to one line.
    assert tool_call_title("zsh", KIND_SHELL, {"command": "ls"}, intent_summary=" Count\nfiles ") == "Count files"
    # Shell fallback: the command's first non-empty line, never "Execute zsh" —
    # the shell tool's name is the bare shell binary and carries no information.
    assert tool_call_title("zsh", KIND_SHELL, {"command": "\n  cloc --vcs=git .  \nwc -l"}) == "cloc --vcs=git ."
    # Filesystem calls are titled by their path, which a chrys parent's
    # approval dialog dedups against the path argument box.
    assert tool_call_title("read_file", KIND_FILESYSTEM_READ, {"path": "a.py"}) == "a.py"
    assert tool_call_title("write_file", KIND_FILESYSTEM_WRITE, {"path": "b.py", "content": "x"}) == "b.py"
    # Fallback humanizes the tool name.
    assert tool_call_title("read_file", KIND_FILESYSTEM_READ, {}) == "read file"
    assert tool_call_title("todo_write", "todo", {"items": []}) == "todo write"
    # Shell without a usable command still falls back to the name.
    assert tool_call_title("zsh", KIND_SHELL, {"script": 1}) == "zsh"
    assert tool_call_title("", "", None) == "Tool call"


def test_tool_call_title_clamp_keeps_containment_probe_prefix_raw() -> None:
    command = "echo " + "x" * 400
    title = tool_call_title("zsh", KIND_SHELL, {"command": command})
    assert len(title) == 160
    assert title.endswith("…")
    # The first 120 chars must stay an exact substring of the command: a chrys
    # parent's approval dialog probes that prefix against the arg values to
    # suppress a title that merely repeats the command argument.
    assert title[:120] in command

    # The humanized tool-name fallback clamps too — the name is remote-chosen.
    fallback = tool_call_title("t" * 400, "", {})
    assert len(fallback) == 160
    assert fallback.endswith("…")


def test_bridge_titles_shell_tool_calls_with_the_command() -> None:
    bridge = AcpEventBridge()

    start = bridge.updates_for_event(
        InvocationToolCallStart(
            tool_name="zsh",
            tool_kind=KIND_SHELL,
            call_id="c1",
            args={"command": "cloc --vcs=git ."},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert start.title == "cloc --vcs=git ."


def test_bridge_ignores_tool_result_images() -> None:
    bridge = AcpEventBridge()

    result = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="view_image",
            call_id="c1",
            result="Image: pixel.png",
            image_contents=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert result.raw_output == "Image: pixel.png"
    assert len(result.content or []) == 1
    assert result.content[0].content.text == "Image: pixel.png"


def test_bridge_projects_hosted_result_images_and_artifacts() -> None:
    # Image-generation and code-interpreter results often carry no text; the
    # live event must project their payloads like persisted history does or
    # the card completes empty until a session reload.
    bridge = AcpEventBridge()

    result = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="image_generation",
            call_id="img_1",
            result="",
            provider_hosted=True,
            image_contents=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            artifacts=[{"id": "file_1", "path": "https://files.test/report.csv", "mime": "text/csv", "size": 12}],
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    blocks = result.content or []
    assert len(blocks) == 2
    image = blocks[0].content
    assert image.type == "image"
    assert image.data == "AAA"
    assert image.mime_type == "image/png"
    link = blocks[1].content
    assert link.type == "resource_link"
    assert link.uri == "https://files.test/report.csv"
    assert link.mime_type == "text/csv"
    assert link.size == 12


@pytest.mark.asyncio
async def test_bridge_hosted_result_round_trips_metadata_and_structured_content() -> None:
    bridge = AcpEventBridge()
    bus = EventBus()
    starts: list[InvocationToolCallStart] = []
    results: list[InvocationToolCallResult] = []

    async def capture_start(event: InvocationToolCallStart) -> None:
        starts.append(event)

    async def capture_result(event: InvocationToolCallResult) -> None:
        results.append(event)

    await bus.subscribe(InvocationToolCallStart, capture_start)
    await bus.subscribe(InvocationToolCallResult, capture_result)
    translator = AcpUpdateTranslator(
        event_bus=bus,
        session_id="parent",
        agent_name="Nested Chrys",
        invocation_id="inv_1",
        attempt=1,
        origin=InvocationOrigin("sub_agent", "parent", "inv_1", None),
    )
    events = [
        InvocationToolCallStart(
            tool_name="image_generation",
            tool_kind="search",
            call_id="hosted:1",
            provider_hosted=True,
            hosted_family="image",
            provider="openai",
            provider_item_type="image_generation_call",
            provider_call_id="provider-image-1",
            provider_status="running",
            origin=InvocationOrigin("turn", "", "turn-test", None),
        ),
        InvocationToolCallResult(
            tool_name="image_generation",
            call_id="hosted:1",
            provider_hosted=True,
            hosted_family="image",
            provider="openai",
            provider_item_type="image_generation_call",
            provider_call_id="provider-image-1",
            provider_status="completed",
            image_contents=[Content.from_uri("data:image/png;base64,QUJD", media_type="image/png")],
            artifacts=[
                {
                    "name": "report.csv",
                    "path": "https://files.test/report.csv",
                    "mime": "text/csv",
                    "size": 12,
                }
            ],
            origin=InvocationOrigin("turn", "", "turn-test", None),
        ),
    ]
    for seq, event in enumerate(events, start=1):
        update = bridge.updates_for_event(event)[0]
        await translator.put(seq, SessionNotification(sessionId="nested", update=update))

    assert starts[-1].provider_hosted is True
    assert starts[-1].hosted_family == "image"
    assert starts[-1].provider == "openai"
    assert starts[-1].provider_item_type == "image_generation_call"
    assert starts[-1].provider_call_id == "provider-image-1"
    assert results[-1].provider_status == "completed"
    assert results[-1].image_contents[0].uri == "data:image/png;base64,QUJD"
    assert results[-1].image_contents[0].media_type == "image/png"
    assert results[-1].artifacts == [
        {
            "name": "report.csv",
            "path": "https://files.test/report.csv",
            "mime": "text/csv",
            "size": 12,
        }
    ]


def test_bridge_projects_hosted_progress_partial_images() -> None:
    # Partial image snapshots ride ToolCallProgress; a transport failure after
    # this point yields only a failed status update, so the images must reach
    # the client now or never.
    bridge = AcpEventBridge()

    progress = bridge.updates_for_event(
        InvocationToolCallProgress(
            call_id="img_1",
            lines=["Generating"],
            provider_hosted=True,
            image_contents=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    blocks = progress.content or []
    assert len(blocks) == 2
    assert blocks[0].content.text == "Generating"
    assert blocks[1].content.type == "image"
    assert blocks[1].content.data == "AAA"
    assert blocks[1].content.mime_type == "image/png"


def test_bridge_text_progress_preserves_pushed_partial_images() -> None:
    bridge = AcpEventBridge()
    bridge.updates_for_event(
        InvocationToolCallProgress(
            call_id="img_1",
            lines=["Generating"],
            provider_hosted=True,
            image_contents=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )

    progress = bridge.updates_for_event(
        InvocationToolCallProgress(
            call_id="img_1",
            lines=["Still working"],
            provider_hosted=True,
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    blocks = progress.content or []
    assert len(blocks) == 2
    assert blocks[0].content.text == "Still working"
    assert blocks[1].content.type == "image"
    assert blocks[1].content.data == "AAA"


def test_bridge_failed_status_preserves_pushed_partial_images() -> None:
    # An update's content field replaces the card's whole collection: the
    # transport-failure path (progress with images, then a failed status
    # carrying only error text) must re-send the images or they vanish.
    bridge = AcpEventBridge()
    bridge.updates_for_event(
        InvocationToolCallProgress(
            call_id="img_1",
            lines=["Generating"],
            provider_hosted=True,
            image_contents=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )

    failed = bridge.updates_for_event(
        InvocationToolCallStatusUpdated(
            call_id="img_1",
            status="failed",
            provider_status="failed",
            provider_hosted=True,
            metadata={"result_text": "Error: connection lost"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    blocks = failed.content or []
    assert failed.status == "failed"
    assert len(blocks) == 2
    assert blocks[0].content.text == "Error: connection lost"
    assert blocks[1].content.type == "image"
    assert blocks[1].content.data == "AAA"


def test_bridge_sub_agent_failed_status_keeps_parent_running_and_preserves_partial_images() -> None:
    bridge = AcpEventBridge()
    bridge.updates_for_event(
        InvocationStarted(
            agent_name="Explore",
            parent_call_id="c1",
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )
    bridge.updates_for_event(
        InvocationToolCallProgress(
            agent_name="Explore",
            tool_name="image_generation",
            call_id="hosted:1",
            lines=["Generating"],
            provider_hosted=True,
            image_contents=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )

    failed = bridge.updates_for_event(
        InvocationToolCallStatusUpdated(
            agent_name="Explore",
            tool_name="image_generation",
            call_id="hosted:1",
            status="failed",
            provider_hosted=True,
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )[0]

    blocks = failed.content or []
    assert failed.status == "in_progress"
    assert len(blocks) == 2
    assert blocks[1].content.type == "image"
    assert blocks[1].content.data == "AAA"

    result = bridge.updates_for_event(
        InvocationToolCallResult(
            agent_name="Explore",
            tool_name="image_generation",
            call_id="hosted:1",
            result="Error: generation failed",
            provider_hosted=True,
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )[0]
    assert result.status == "in_progress"


def test_bridge_sub_agent_hosted_blocks_survive_later_notes_until_parent_result() -> None:
    # All sub-agent notes replace the same parent card's content collection:
    # accumulated hosted blocks must ride every later note (paused, stats)
    # and the final parent result, then be dropped with the invocation.
    bridge = AcpEventBridge()
    bridge.updates_for_event(
        InvocationStarted(
            agent_name="Explore",
            parent_call_id="c1",
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )
    bridge.updates_for_event(
        InvocationToolCallProgress(
            agent_name="Explore",
            tool_name="image_generation",
            call_id="hosted:1",
            lines=["Generating"],
            provider_hosted=True,
            image_contents=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )
    bridge.updates_for_event(
        InvocationToolCallStatusUpdated(
            agent_name="Explore",
            tool_name="image_generation",
            call_id="hosted:1",
            status="failed",
            provider_hosted=True,
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )

    paused = bridge.updates_for_event(
        InvocationPaused(
            agent_name="Explore",
            reason="stall",
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )[0]
    assert paused.status == "in_progress"
    assert [block.content.type for block in paused.content] == ["text", "image"]
    assert paused.content[1].content.data == "AAA"

    bridge.updates_for_event(
        InvocationToolCallResult(
            agent_name="Explore",
            tool_name="code_interpreter",
            call_id="hosted:2",
            result="csv written",
            provider_hosted=True,
            image_contents=[Content.from_uri("data:image/png;base64,BBB", media_type="image/png")],
            artifacts=[{"id": "file_1", "name": "report.csv", "mime": "text/csv"}],
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )
    stats = bridge.updates_for_event(
        InvocationProgress(agent_name="Explore", origin=InvocationOrigin("sub_agent", "", "inv_1", None))
    )[0]
    assert [block.content.type for block in stats.content] == ["text", "image", "image", "text"]
    assert stats.content[1].content.data == "AAA"
    assert stats.content[2].content.data == "BBB"
    assert stats.content[3].content.text == "Hosted artifact: report.csv (text/csv)"

    parent = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="explore",
            call_id="c1",
            result="summary",
            metadata={"sub_agent_invocation_id": "inv_1"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]
    assert [block.content.type for block in parent.content] == ["text", "image", "image", "text"]
    assert parent.content[0].content.text == "summary"

    assert (
        bridge.updates_for_event(
            InvocationProgress(agent_name="Explore", origin=InvocationOrigin("sub_agent", "", "inv_1", None))
        )
        == []
    )


def test_bridge_sub_agent_blocks_do_not_leak_across_reused_parent_call_ids() -> None:
    # call_ids are per-run counters, so two concurrent sub-agents can share a
    # parent call_id; notes, the final projection, and cleanup must all key on
    # the invocation, never the parent call_id.
    bridge = AcpEventBridge()
    bridge.updates_for_event(
        InvocationStarted(
            agent_name="A",
            parent_call_id="c1",
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )
    bridge.updates_for_event(
        InvocationStarted(
            agent_name="B",
            parent_call_id="c1",
            origin=InvocationOrigin("sub_agent", "", "inv_2", None),
        )
    )
    for invocation_id, data in (("inv_1", "AAA"), ("inv_2", "BBB")):
        bridge.updates_for_event(
            InvocationToolCallProgress(
                agent_name="X",
                tool_name="image_generation",
                call_id="hosted:1",
                lines=["Generating"],
                provider_hosted=True,
                image_contents=[Content.from_uri(f"data:image/png;base64,{data}", media_type="image/png")],
                origin=InvocationOrigin("sub_agent", "", invocation_id or "event-test", None),
            )
        )

    note_1 = bridge.updates_for_event(
        InvocationProgress(agent_name="A", origin=InvocationOrigin("sub_agent", "", "inv_1", None))
    )[0]
    note_2 = bridge.updates_for_event(
        InvocationProgress(agent_name="B", origin=InvocationOrigin("sub_agent", "", "inv_2", None))
    )[0]
    assert [block.content.data for block in note_1.content[1:]] == ["AAA"]
    assert [block.content.data for block in note_2.content[1:]] == ["BBB"]

    parent_1 = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="a",
            call_id="c1",
            result="one",
            metadata={"sub_agent_invocation_id": "inv_1"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]
    assert [block.content.data for block in parent_1.content[1:]] == ["AAA"]

    survivor = bridge.updates_for_event(
        InvocationProgress(agent_name="B", origin=InvocationOrigin("sub_agent", "", "inv_2", None))
    )[0]
    assert [block.content.data for block in survivor.content[1:]] == ["BBB"]

    parent_2 = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="b",
            call_id="c1",
            result="two",
            metadata={"sub_agent_invocation_id": "inv_2"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]
    assert [block.content.data for block in parent_2.content[1:]] == ["BBB"]
    assert (
        bridge.updates_for_event(
            InvocationProgress(agent_name="B", origin=InvocationOrigin("sub_agent", "", "inv_2", None))
        )
        == []
    )


def test_bridge_metadata_less_sibling_result_does_not_drop_reused_parent_call_id() -> None:
    bridge = AcpEventBridge()
    bridge.updates_for_event(
        InvocationStarted(
            agent_name="A",
            parent_call_id="c1",
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )
    bridge.updates_for_event(
        InvocationToolCallProgress(
            agent_name="A",
            tool_name="image_generation",
            call_id="hosted:1",
            provider_hosted=True,
            image_contents=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )

    # A non-sub-agent sibling can legally reuse the parent call_id.  With no
    # exact invocation metadata, the bridge must fail closed rather than guess
    # ownership and tear down the live sub-agent.
    bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="shell", call_id="c1", result="ok", origin=InvocationOrigin("turn", "", "turn-test", None)
        )
    )

    survivor = bridge.updates_for_event(
        InvocationProgress(agent_name="A", origin=InvocationOrigin("sub_agent", "", "inv_1", None))
    )[0]
    assert [block.content.data for block in survivor.content[1:]] == ["AAA"]


def test_bridge_keeps_local_progress_text_only() -> None:
    bridge = AcpEventBridge()

    progress = bridge.updates_for_event(
        InvocationToolCallProgress(
            call_id="c1",
            lines=["running"],
            image_contents=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert len(progress.content or []) == 1
    assert progress.content[0].content.text == "running"


def test_bridge_projects_sub_agent_hosted_images_and_artifacts() -> None:
    bridge = AcpEventBridge()
    bridge.updates_for_event(
        InvocationStarted(
            agent_name="Explore",
            parent_call_id="c1",
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )

    progress = bridge.updates_for_event(
        InvocationToolCallProgress(
            agent_name="Explore",
            tool_name="image_generation",
            lines=["Generating"],
            provider_hosted=True,
            image_contents=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )[0]
    result = bridge.updates_for_event(
        InvocationToolCallResult(
            agent_name="Explore",
            tool_name="code_interpreter",
            result="done",
            provider_hosted=True,
            image_contents=[Content.from_uri("data:image/png;base64,BBB", media_type="image/png")],
            artifacts=[{"id": "file_1", "name": "report.csv", "mime": "text/csv"}],
            origin=InvocationOrigin("sub_agent", "", "inv_1", None),
        )
    )[0]

    assert progress.content[1].content.type == "image"
    assert progress.content[1].content.data == "AAA"
    assert result.content[1].content.type == "image"
    assert result.content[1].content.data == "BBB"
    assert result.content[2].content.type == "text"
    assert result.content[2].content.text == "Hosted artifact: report.csv (text/csv)"


def test_bridge_renders_name_only_artifacts_as_text_not_links() -> None:
    # OpenAI hosted_file artifacts have a filename but no URI; a resource_link
    # with uri="report.csv" would be an unresolvable link.
    bridge = AcpEventBridge()

    result = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="code_interpreter",
            call_id="code_1",
            result="",
            provider_hosted=True,
            artifacts=[
                {"id": "file_1", "name": "report.csv", "mime": "text/csv"},
                {"name": "plot.csv", "path": "https://files.test/plot.csv", "mime": "text/csv"},
            ],
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    blocks = result.content or []
    assert len(blocks) == 2
    assert blocks[0].content.type == "text"
    assert blocks[0].content.text == "Hosted artifact: report.csv (text/csv)"
    link = blocks[1].content
    assert link.type == "resource_link"
    assert link.name == "plot.csv"
    assert link.uri == "https://files.test/plot.csv"


def test_bridge_marks_error_tool_results_failed() -> None:
    bridge = AcpEventBridge()

    bridge.updates_for_event(
        InvocationToolCallStart(
            tool_name="read_file",
            tool_kind=KIND_FILESYSTEM_READ,
            call_id="c1",
            args={},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )
    result = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="read_file",
            call_id="c1",
            result="Error: nope",
            metadata={},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert result.status == "failed"


def test_a_steer_fails_the_turns_tool_calls_still_open_and_only_those() -> None:
    """A steer cancels the running calls with no result to follow, and the prompt goes on: the bridge ends their cards."""
    bridge = AcpEventBridge()
    for call_id in ("done", "hosted", "running", "cancelled"):
        bridge.updates_for_event(InvocationToolCallStart(tool_name="bash", call_id=call_id, args={}, origin=_TURN))
    bridge.updates_for_event(InvocationToolCallResult(tool_name="bash", call_id="done", result="ok", origin=_TURN))
    bridge.updates_for_event(
        InvocationToolCallStatusUpdated(call_id="hosted", status="completed", provider_hosted=True, origin=_TURN)
    )
    child = InvocationOrigin("sub_agent", "", "child", _TURN)
    bridge.updates_for_event(InvocationToolCallStart(tool_name="bash", call_id="child-call", args={}, origin=child))

    ended = bridge.updates_for_event(InvocationSteered(origin=_TURN))

    assert [(update.tool_call_id, update.status) for update in ended] == [
        ("running", "failed"),
        ("cancelled", "failed"),
    ]
    # The next pass's calls are its own: a later steer ends only those.
    bridge.updates_for_event(InvocationToolCallStart(tool_name="bash", call_id="next", args={}, origin=_TURN))
    assert [update.tool_call_id for update in bridge.updates_for_event(InvocationSteered(origin=_TURN))] == ["next"]


def test_bridge_marks_structured_tool_failures_failed() -> None:
    bridge = AcpEventBridge()

    result = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="bash",
            call_id="c1",
            result="Permission denied",
            metadata={"approval": "user_rejected"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert result.status == "failed"


def test_bridge_honors_structured_success_before_error_text() -> None:
    bridge = AcpEventBridge()

    result = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="external_tool",
            call_id="c1",
            result="Error: expected text",
            metadata={TOOL_FAILED_METADATA_KEY: False},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert result.status == "completed"


def test_bridge_hard_failure_beats_structured_success() -> None:
    bridge = AcpEventBridge()

    errored = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="external_tool",
            call_id="c1",
            result="ok",
            metadata={TOOL_FAILED_METADATA_KEY: False, TOOL_ERRORED_METADATA_KEY: True},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]
    process_failed = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="external_tool",
            call_id="c2",
            result="ok",
            metadata={TOOL_FAILED_METADATA_KEY: False, PROCESS_EXIT_CODE_METADATA_KEY: 1},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert errored.status == "failed"
    assert process_failed.status == "failed"


def test_bridge_applies_error_text_fallback_to_mcp_tools() -> None:
    bridge = AcpEventBridge()

    bridge.updates_for_event(
        InvocationToolCallStart(
            tool_name="remote",
            tool_kind=KIND_MCP,
            call_id="c1",
            args={},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )
    result = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="remote",
            call_id="c1",
            result="Error: remote domain text",
            metadata={},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert result.status == "failed"


def test_bridge_uses_structured_metadata_for_unkinded_skill_and_context_tools() -> None:
    bridge = AcpEventBridge()

    skill = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="run_skill_script",
            call_id="c1",
            result="Error: script failed",
            metadata={TOOL_FAILED_METADATA_KEY: True},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]
    context = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="compress_context",
            call_id="c2",
            result="Error: nothing to compress",
            metadata={TOOL_FAILED_METADATA_KEY: True},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert skill.status == "failed"
    assert context.status == "failed"


def test_bridge_applies_mcp_kind_before_known_chrys_name_fallback() -> None:
    bridge = AcpEventBridge()

    bridge.updates_for_event(
        InvocationToolCallStart(
            tool_name="run_skill_script",
            tool_kind=KIND_MCP,
            call_id="c1",
            args={},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )
    result = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="run_skill_script",
            call_id="c1",
            result="Error: remote domain text",
            metadata={},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert result.status == "failed"


def test_bridge_honors_structured_success_before_mcp_error_text() -> None:
    bridge = AcpEventBridge()

    bridge.updates_for_event(
        InvocationToolCallStart(
            tool_name="remote",
            tool_kind=KIND_MCP,
            call_id="c1",
            args={},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )
    result = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="remote",
            call_id="c1",
            result="Error: expected remote payload",
            metadata={TOOL_FAILED_METADATA_KEY: False},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert result.status == "completed"


def test_bridge_uses_shell_result_metadata_for_status() -> None:
    bridge = AcpEventBridge()

    failed = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="bash",
            call_id="c1",
            result="boom\n[exit_code: 1]",
            metadata={SHELL_EXIT_CODE_METADATA_KEY: 1},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]
    succeeded = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="bash",
            call_id="c2",
            result="Error: expected stdout",
            metadata={SHELL_EXIT_CODE_METADATA_KEY: 0},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]
    timed_out = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="bash",
            call_id="c3",
            result="partial output",
            metadata={SHELL_TIMED_OUT_METADATA_KEY: True},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert failed.status == "failed"
    assert succeeded.status == "completed"
    assert timed_out.status == "failed"


def test_bridge_uses_legacy_shell_exit_suffix_for_status_without_metadata() -> None:
    bridge = AcpEventBridge()

    bridge.updates_for_event(
        InvocationToolCallStart(
            tool_name="run_command",
            tool_kind=KIND_SHELL,
            call_id="c1",
            args={"command": "printf"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )
    succeeded = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="run_command",
            call_id="c1",
            result="Error: expected stdout\n[exit_code: 0]",
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]
    bridge.updates_for_event(
        InvocationToolCallStart(
            tool_name="run_command",
            tool_kind=KIND_SHELL,
            call_id="c2",
            args={"command": "false"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )
    failed = bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="run_command",
            call_id="c2",
            result="boom\n[exit_code: 1]",
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert succeeded.status == "completed"
    assert failed.status == "failed"


def test_bridge_maps_usage_and_session_info() -> None:
    bridge = AcpEventBridge()

    usage = bridge.updates_for_event(UsageUpdate(total_tokens=25, max_context_tokens=100))[0]
    info = bridge.updates_for_event(SessionSaved(session_id="s"))[0]

    assert usage.session_update == "usage_update"
    assert usage.used == 25
    assert usage.size == 100
    assert info.session_update == "session_info_update"
    assert info.updated_at is not None


def test_bridge_maps_session_title_updates() -> None:
    from chrys.foundation.events.types import SessionTitleUpdated

    bridge = AcpEventBridge()

    titled = bridge.updates_for_event(
        SessionTitleUpdated(session_id="s", title="Login bug fix", custom=False, display_title="Login bug fix")
    )[0]
    assert titled.session_update == "session_info_update"
    assert titled.title == "Login bug fix"
    assert titled.updated_at is not None

    # Clearing a custom title keeps the resolved fallback (generated or
    # first-message title) instead of clearing the client's label.
    cleared = bridge.updates_for_event(
        SessionTitleUpdated(session_id="s", title="", custom=True, display_title="Auto topic")
    )[0]
    assert cleared.title == "Auto topic"

    # Only a session with no title at all maps to null per ACP
    # ("set to null to clear").
    bare = bridge.updates_for_event(SessionTitleUpdated(session_id="s", title="", custom=True))[0]
    assert bare.title is None


def test_bridge_maps_todo_list_updated_to_plan() -> None:
    bridge = AcpEventBridge()
    items = [
        TodoItem(content="write tests", status="completed", active_form="writing tests"),
        TodoItem(content="run gates", status="in_progress"),
        TodoItem(content="ship", status="pending"),
    ]

    updates = bridge.updates_for_event(TodoListUpdated(items=items, session_id="s"))

    assert len(updates) == 1
    plan = updates[0]
    assert plan.session_update == "plan"
    assert [(entry.content, entry.status, entry.priority) for entry in plan.entries] == [
        ("write tests", "completed", "medium"),
        ("run gates", "in_progress", "medium"),
        ("ship", "pending", "medium"),
    ]


def test_bridge_maps_empty_todo_list_to_plan_clear() -> None:
    """An empty list still yields one plan update — it clears the client plan."""
    bridge = AcpEventBridge()

    updates = bridge.updates_for_event(TodoListUpdated(items=[], session_id="s"))

    assert len(updates) == 1
    assert updates[0].session_update == "plan"
    assert updates[0].entries == []


def test_plan_update_for_todos_accepts_tuple_snapshot() -> None:
    """The server seeds plans straight from ``TodoTracker.snapshot()`` tuples."""
    update = plan_update_for_todos((TodoItem(content="a"),))

    assert update.session_update == "plan"
    assert update.entries[0].content == "a"
    assert update.entries[0].status == "pending"


def test_bridge_folds_sub_agent_updates_into_parent_tool_call() -> None:
    bridge = AcpEventBridge()

    start = bridge.updates_for_event(
        InvocationStarted(
            agent_name="Explore",
            tool_name="explore",
            parent_call_id="parent",
            origin=InvocationOrigin("sub_agent", "", "i1", None),
        )
    )[0]
    progress = bridge.updates_for_event(
        InvocationProgress(
            agent_name="Explore",
            tool_call_count=2,
            total_tokens=42,
            total_usage_tokens=84,
            usage_unreported_attempts=1,
            origin=InvocationOrigin("sub_agent", "", "i1", None),
        )
    )[0]

    assert start.tool_call_id == "parent"
    assert start.status == "in_progress"
    assert "Explore started" in start.raw_output
    assert progress.tool_call_id == "parent"
    assert "2 tool call" in progress.raw_output
    assert "42 context token" in progress.raw_output
    assert "84 total token" in progress.raw_output
    assert "1 unreported attempt" in progress.raw_output


@pytest.mark.parametrize(
    ("result", "note"),
    [
        ("\n  first line\nsecond line", "Sub-agent Explore completed tool read_file: first line"),
        ("x" * 500, "Sub-agent Explore completed tool read_file: " + "x" * 159 + "…"),
        ("", "Sub-agent Explore completed tool read_file."),
    ],
)
def test_a_nested_tool_result_shows_only_its_first_line_on_the_parent_card(result: str, note: str) -> None:
    """Each note replaces the card's content; the full result travels in the extension notification."""
    bridge = AcpEventBridge()
    origin = InvocationOrigin("sub_agent", "", "i1", None)
    bridge.updates_for_event(
        InvocationStarted(agent_name="Explore", tool_name="explore", parent_call_id="parent", origin=origin)
    )

    [update] = bridge.updates_for_event(
        InvocationToolCallResult(
            agent_name="Explore", tool_name="read_file", call_id="c1", result=result, origin=origin
        )
    )

    assert update.tool_call_id == "parent"
    assert update.raw_output == note
    assert [block.content.text for block in update.content] == [note]


def test_bridge_folds_sub_agent_compaction_into_parent_tool_call() -> None:
    bridge = AcpEventBridge()
    bridge.updates_for_event(
        InvocationStarted(
            agent_name="Explore",
            tool_name="explore",
            parent_call_id="parent",
            origin=InvocationOrigin("sub_agent", "", "i1", None),
        )
    )

    started = bridge.updates_for_event(
        InvocationCompactionStarted(
            agent_name="Explore",
            compaction_id="c-1",
            origin=InvocationOrigin("sub_agent", "", "i1", None),
        )
    )[0]
    finished = bridge.updates_for_event(
        InvocationCompactionFinished(
            agent_name="Explore",
            compaction_id="c-1",
            outcome="ok",
            format_violation='missing required heading "## Next"',
            origin=InvocationOrigin("sub_agent", "", "i1", None),
        )
    )[0]
    failed = bridge.updates_for_event(
        InvocationCompactionFinished(
            agent_name="Explore",
            compaction_id="c-2",
            outcome="failed",
            origin=InvocationOrigin("sub_agent", "", "i1", None),
        )
    )[0]

    assert started.tool_call_id == "parent"
    assert "compacting conversation" in started.raw_output
    assert finished.tool_call_id == "parent"
    assert "compacted conversation" in finished.raw_output
    assert 'Summary format warning: missing required heading "## Next"' in finished.raw_output
    assert "compaction failed" in failed.raw_output
    # The committed signal is bookkeeping — no extra human-facing note.
    assert (
        bridge.updates_for_event(
            InvocationCompactionCommitted(
                agent_name="Explore",
                compaction_id="c-1",
                origin=InvocationOrigin("sub_agent", "", "i1", None),
            )
        )
        == []
    )


def test_bridge_drops_sub_agent_parent_after_terminal_update() -> None:
    bridge = AcpEventBridge()

    bridge.updates_for_event(
        InvocationStarted(
            agent_name="Explore",
            tool_name="explore",
            parent_call_id="parent",
            origin=InvocationOrigin("sub_agent", "", "i1", None),
        )
    )
    bridge.updates_for_event(
        InvocationToolCallResult(
            tool_name="Explore",
            call_id="parent",
            result="done",
            metadata={"sub_agent_invocation_id": "i1"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )

    assert (
        bridge.updates_for_event(
            InvocationProgress(
                agent_name="Explore",
                tool_call_count=3,
                total_tokens=99,
                origin=InvocationOrigin("sub_agent", "", "i1", None),
            )
        )
        == []
    )


def test_bridge_drops_sub_agent_parent_after_abort() -> None:
    bridge = AcpEventBridge()

    bridge.updates_for_event(
        InvocationStarted(
            agent_name="Explore",
            tool_name="explore",
            parent_call_id="parent",
            origin=InvocationOrigin("sub_agent", "", "i1", None),
        )
    )
    abort = bridge.updates_for_event(
        InvocationAborted(
            agent_name="Explore",
            last_error="failed",
            origin=InvocationOrigin("sub_agent", "", "i1", None),
        )
    )

    assert abort[0].tool_call_id == "parent"
    assert (
        bridge.updates_for_event(
            InvocationProgress(
                agent_name="Explore",
                tool_call_count=3,
                total_tokens=99,
                origin=InvocationOrigin("sub_agent", "", "i1", None),
            )
        )
        == []
    )


def test_bridge_maps_shell_kind_to_execute() -> None:
    bridge = AcpEventBridge()

    start = bridge.updates_for_event(
        InvocationToolCallStart(
            tool_name="bash",
            tool_kind=KIND_SHELL,
            call_id="c1",
            args={"command": "ls"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert start.kind == "execute"


def test_bridge_maps_sub_agent_kind_to_other() -> None:
    bridge = AcpEventBridge()

    start = bridge.updates_for_event(
        InvocationToolCallStart(
            tool_name="Explore",
            tool_kind=KIND_SUB_AGENT,
            call_id="c1",
            args={"prompt": "inspect"},
            origin=InvocationOrigin("turn", "", "turn-test", None),
        )
    )[0]

    assert start.kind == "other"
