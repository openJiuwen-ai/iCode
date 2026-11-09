# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for consecutive tool-group coalescing.

The rule (shared by live rendering and replay): consecutive tool calls —
single or parallel — merge into one :class:`ToolGroup` until the agent
produces non-empty intermediate text (or a non-tool boundary such as a
user message).  Batch size is no longer a grouping signal; only actual
prose between tools starts a new group.

Covers:
- Live rendering: every tool appended to the last group unless text breaks it
- Live rendering: intermediate text breaks the merge chain
- Replay: same coalescing behavior is reproduced from serialized messages
- Replay → live equivalence for complex mixed scenarios
"""

from __future__ import annotations

import asyncio
from itertools import count
from typing import Any

import pytest
from textual.app import App, ComposeResult

from chrys.app.tui.widgets.chat.messages import AgentMessage as AgentMessageWidget
from chrys.app.tui.widgets.chat.messages import UserMessage as UserMessageWidget
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.foundation.events.types import (
    InvocationContentDelta,
    InvocationMessage,
    InvocationToolCallResult,
    InvocationToolCallStart,
)
from chrys.foundation.hosted_tools import HostedRetrySafety, HostedToolPhase
from chrys.kernel import Content, Message
from chrys.service.llm.mock import MockChatClient, MockResponse
from tests.support.pipeline_helpers import create_test_engine

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")

# ---------------------------------------------------------------------------
# Helpers — replay message construction (mirrors test_replay_batch_merge.py)
# ---------------------------------------------------------------------------


def _fc(call_id: str, name: str) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def _fr(call_id: str, result: str = "ok") -> dict[str, Any]:
    return {"type": "function_result", "call_id": call_id, "result": result}


def _text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def _asst_msg(
    contents: list[dict],
    *,
    batch_id: int | None = None,
    intermediate_text: str | None = None,
    approval: dict[str, str] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "contents": contents}
    ap: dict[str, Any] = {}
    if batch_id is not None:
        ap["_batch_id"] = batch_id
    if intermediate_text is not None:
        ap["_intermediate_text"] = intermediate_text
    if approval is not None:
        ap["_approval"] = approval
    if ap:
        msg["additional_properties"] = ap
    return msg


def _tool_msg(contents: list[dict]) -> dict[str, Any]:
    return {"role": "tool", "contents": contents}


def _user_msg(text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "contents": [_text(text)],
        "additional_properties": {"_group": {"id": f"g_{text}"}},
    }


# ---------------------------------------------------------------------------
# Helpers — inspect widget tree
# ---------------------------------------------------------------------------


def _snapshot_groups(cp: ChatPanel) -> list[tuple[int, list[str]]]:
    """Return [(tool_count, [tool_names])] for each ToolGroup in mount order."""
    result = []
    for tg in cp.query(ToolGroup):
        names = [record.tool_name for record in tg._tool_records.values()]
        result.append((len(tg._tool_records), names))
    return result


def _group_sizes(cp: ChatPanel) -> list[int]:
    return [len(tg._tool_records) for tg in cp.query(ToolGroup)]


def _intermediate_texts(cp: ChatPanel) -> list[str]:
    return [m.text for m in cp.query(AgentMessageWidget) if m._is_intermediate]


# ---------------------------------------------------------------------------
# Helpers — simulate live event sequence
# ---------------------------------------------------------------------------

_SEGMENT_IDS = count()


async def _sim_answer_segment(cp: ChatPanel, text: str) -> None:
    """Stream *text* as one answer segment, closed as the backend closes it."""
    segment_id = f"segment-{next(_SEGMENT_IDS)}"
    await cp.add_answer_delta(segment_id, text)
    cp.finish_answer_segment(segment_id, completed=True)


async def _sim_batch_boundary(cp: ChatPanel) -> None:
    """Simulate a response whose only text before its tool calls is whitespace."""
    await _sim_answer_segment(cp, "\n\n")


async def _sim_intermediate_text(cp: ChatPanel, text: str) -> None:
    """Simulate answer text that tool calls then follow."""
    await _sim_answer_segment(cp, text)


async def _sim_tool(cp: ChatPanel, call_id: str, name: str, duration_ms: int = 50) -> None:
    """Simulate a single tool call start + result."""
    await cp.add_tool_start(call_id, name, '{"arg": "val"}')
    await cp.add_tool_result(call_id, name, "ok", duration_ms)


async def _sim_final(cp: ChatPanel, text: str = "Done.") -> None:
    """Simulate the streamed final answer and the message that completes it."""
    await _sim_answer_segment(cp, text)
    await cp.add_agent_message(text)


class _PanelApp(App):
    def compose(self) -> ComposeResult:
        yield ChatPanel()


# ---------------------------------------------------------------------------
# Live rendering tests
# ---------------------------------------------------------------------------


class TestLiveCoalescing:
    """Test that consecutive single-tool batches merge into one ToolGroup
    during live rendering (add_tool_start / add_agent_message calls).
    """

    @pytest.mark.asyncio
    async def test_consecutive_singles_merge(self) -> None:
        """3 consecutive single-tool batches → 1 ToolGroup with 3 tools."""
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            # Batch 1: single tool
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c1", "read_file")
            # Batch 2: single tool
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c2", "grep")
            # Batch 3: single tool
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c3", "edit_file")

            await _sim_final(cp)

            assert _group_sizes(cp) == [3]
            snap = _snapshot_groups(cp)
            assert snap[0][1] == ["read_file", "grep", "edit_file"]

    @pytest.mark.asyncio
    async def test_parallel_batch_merges_with_neighbors(self) -> None:
        """Parallel tool calls merge into the surrounding group when no text separates them."""
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            # Batch 1: single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c1", "read_file")
            # Batch 2: parallel (3 tools)
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c2", "glob")
            await _sim_tool(cp, "c3", "glob")
            await _sim_tool(cp, "c4", "glob")
            # Batch 3: single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c5", "edit_file")

            await _sim_final(cp)

            assert _group_sizes(cp) == [5]

    @pytest.mark.asyncio
    async def test_intermediate_text_breaks_merge(self) -> None:
        """Non-empty intermediate text separates tool groups."""
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            # Batch 1: single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c1", "read_file")
            # Intermediate text + batch 2: single
            await _sim_intermediate_text(cp, "Let me search for that")
            await _sim_tool(cp, "c2", "grep")
            # Batch 3: single (no text → merges with batch 2)
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c3", "edit_file")

            await _sim_final(cp)

            assert _group_sizes(cp) == [1, 2]
            assert _intermediate_texts(cp) == ["Let me search for that"]

    @pytest.mark.asyncio
    async def test_blocking_mixed_hosted_and_local_pipeline_matches_replay_grouping(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        """Non-streaming response acceptance must split live groups before the next response."""

        def hosted_searches(prefix: str, count: int) -> list[Content]:
            contents: list[Content] = []
            for index in range(count):
                call_id = f"{prefix}_{index}"
                contents.extend(
                    [
                        Content.from_search_tool_call(
                            call_id,
                            tool_name="web_search",
                            arguments={"query": call_id},
                            status="completed",
                            hosted_provider="openai",
                            provider_item_type="web_search_call",
                            provider_item_id=call_id,
                            provider_phase=HostedToolPhase.TERMINAL,
                            provider_status="completed",
                            retry_safety=HostedRetrySafety.READ_ONLY,
                        ),
                        Content.from_search_tool_result(
                            call_id,
                            tool_name="web_search",
                            result={"query": call_id},
                            status="completed",
                            hosted_provider="openai",
                            provider_item_type="web_search_call",
                            provider_item_id=call_id,
                            provider_phase=HostedToolPhase.TERMINAL,
                            provider_status="completed",
                            retry_safety=HostedRetrySafety.READ_ONLY,
                        ),
                    ]
                )
            return contents

        first_response = MockResponse(
            tool_calls=[("echo", "local_1", {"message": "details"})],
            finish_reason="tool_calls",
        )
        second_response = MockResponse(text="Final answer.")
        scripted_contents = {
            id(first_response): [
                *hosted_searches("first", 10),
                Content.from_text("Checking details."),
                Content.from_function_call("local_1", "echo", arguments={"message": "details"}),
            ],
            id(second_response): [*hosted_searches("second", 20), Content.from_text("Final answer.")],
        }
        original_build_messages = MockChatClient._build_messages

        def build_messages(client: MockChatClient, response: MockResponse) -> list[Message]:
            contents = scripted_contents.get(id(response))
            if contents is None:
                return original_build_messages(client, response)
            return [Message(role="assistant", contents=contents)]

        monkeypatch.setattr(MockChatClient, "_build_messages", build_messages)

        context = await create_test_engine(
            [first_response, second_response],
            tmp_path,
            approval_default="skip",
            stream=False,
        )

        try:
            async with _PanelApp().run_test() as pilot:
                panel = pilot.app.query_one(ChatPanel)

                async def render_answer(event: InvocationContentDelta) -> None:
                    if event.channel == "answer":
                        await panel.add_answer_delta(event.segment_id, event.text)

                async def render_final(event: InvocationMessage) -> None:
                    if event.is_final:
                        await panel.add_agent_message(event.text)

                async def render_tool_start(event: InvocationToolCallStart) -> None:
                    await panel.add_tool_start(
                        event.call_id,
                        event.tool_name,
                        event.tool_kind,
                        args=event.args,
                        provider_hosted=event.provider_hosted,
                        hosted_family=event.hosted_family,
                        provider=event.provider,
                        provider_item_type=event.provider_item_type,
                        provider_status=event.provider_status,
                        provider_call_id=event.provider_call_id,
                    )

                async def render_tool_result(event: InvocationToolCallResult) -> None:
                    await panel.add_tool_result(
                        event.call_id,
                        event.tool_name,
                        event.result,
                        event.duration_ms,
                        image_contents=event.image_contents,
                        metadata=event.metadata,
                        artifacts=event.artifacts,
                        provider_status=event.provider_status,
                    )

                await context.bus.subscribe(InvocationContentDelta, render_answer)
                await context.bus.subscribe(InvocationMessage, render_final)
                await context.bus.subscribe(InvocationToolCallStart, render_tool_start)
                await context.bus.subscribe(InvocationToolCallResult, render_tool_result)
                await panel.add_user_message("go")

                await context.send_message("go")
                await pilot.pause()

                assert _group_sizes(panel) == [10, 21]
                assert _intermediate_texts(panel) == ["Checking details."]
                assert _snapshot_groups(panel)[1][1].count("echo") == 1
        finally:
            await context.cleanup()

    @pytest.mark.asyncio
    async def test_concurrent_tool_starts_share_one_ready_group(self) -> None:
        """Simultaneous starts must never touch a group before it composes.

        The first start awaits the group mount; a concurrent start that
        observed a published-but-unmounted group would crash in
        ``ToolGroup.add_tool`` (``#tg-content`` not composed yet).
        """
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            await asyncio.gather(
                cp.add_tool_start("c1", "read_file", "filesystem.read"),
                cp.add_tool_start("c2", "grep", "search"),
            )
            await pilot.pause()

            assert _group_sizes(cp) == [2]
            assert _snapshot_groups(cp)[0][1] == ["read_file", "grep"]

    @pytest.mark.asyncio
    async def test_whitespace_only_answer_segment_is_noop(self) -> None:
        """A whitespace-only answer segment must not mount text or split tool groups."""
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            await cp.add_tool_start("c1", "read_file", "filesystem.read")
            await cp.add_answer_delta("blank", " \n\t")
            await cp.add_tool_start("c2", "grep", "search")
            await pilot.pause()

            assert _group_sizes(cp) == [2]
            assert _intermediate_texts(cp) == []

    @pytest.mark.asyncio
    async def test_single_after_parallel_keeps_one_group(self) -> None:
        """Singles following a parallel batch keep appending to the same group."""
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            # Batch 1: parallel (2)
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c1", "glob")
            await _sim_tool(cp, "c2", "glob")
            # Batch 2: single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c3", "read_file")
            # Batch 3: single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c4", "edit_file")

            await _sim_final(cp)

            assert _group_sizes(cp) == [4]

    @pytest.mark.asyncio
    async def test_complex_mixed_scenario(self) -> None:
        """Full complex scenario — only intermediate text splits groups.

        Input sequence (each line = one LLM response):
            parallel(3)
            single
            single
            parallel(2)
            intermediate text
            single
            single
            parallel(3)
            single
            single
            single

        Expected output (only the intermediate text between the 4th and
        5th batches breaks the merge chain):
            ToolGroup(7)   — 3 + 1 + 1 + 2
            intermediate text
            ToolGroup(8)   — 1 + 1 + 3 + 1 + 1 + 1
        """
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            # parallel(3)
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c01", "glob")
            await _sim_tool(cp, "c02", "glob")
            await _sim_tool(cp, "c03", "glob")

            # single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c04", "read_file")
            # single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c05", "grep")

            # parallel(2)
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c06", "glob")
            await _sim_tool(cp, "c07", "glob")

            # intermediate text + single
            await _sim_intermediate_text(cp, "Analyzing results")
            await _sim_tool(cp, "c08", "read_file")
            # single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c09", "edit_file")

            # parallel(3)
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c10", "glob")
            await _sim_tool(cp, "c11", "glob")
            await _sim_tool(cp, "c12", "glob")

            # single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c13", "read_file")
            # single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c14", "edit_file")
            # single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c15", "write_file")

            await _sim_final(cp)

            assert _group_sizes(cp) == [7, 8]
            assert _intermediate_texts(cp) == ["Analyzing results"]

    @pytest.mark.asyncio
    async def test_all_singles_merge_into_one(self) -> None:
        """5 consecutive singles with no parallels or text → 1 group."""
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            for i in range(5):
                await _sim_batch_boundary(cp)
                await _sim_tool(cp, f"c{i}", f"tool_{i}")

            await _sim_final(cp)

            assert _group_sizes(cp) == [5]

    @pytest.mark.asyncio
    async def test_all_parallel_merge_without_text(self) -> None:
        """Consecutive parallel batches with no intermediate text form one group."""
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            # parallel(2)
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c1", "glob")
            await _sim_tool(cp, "c2", "glob")
            # parallel(3)
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c3", "grep")
            await _sim_tool(cp, "c4", "grep")
            await _sim_tool(cp, "c5", "grep")

            await _sim_final(cp)

            assert _group_sizes(cp) == [5]

    @pytest.mark.asyncio
    async def test_intermediate_text_with_parallel_keeps_groups_separate(self) -> None:
        """Intermediate text before a parallel batch still separates correctly."""
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            # single
            await _sim_batch_boundary(cp)
            await _sim_tool(cp, "c1", "read_file")
            # intermediate text + parallel(2)
            await _sim_intermediate_text(cp, "Found something interesting")
            await _sim_tool(cp, "c2", "glob")
            await _sim_tool(cp, "c3", "glob")

            await _sim_final(cp)

            assert _group_sizes(cp) == [1, 2]
            assert _intermediate_texts(cp) == ["Found something interesting"]

    @pytest.mark.asyncio
    async def test_text_before_every_tool_splits_each(self) -> None:
        """Intermediate text before every single tool → each tool in its own group."""
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            await _sim_intermediate_text(cp, "Step 1")
            await _sim_tool(cp, "c1", "read_file")
            await _sim_intermediate_text(cp, "Step 2")
            await _sim_tool(cp, "c2", "grep")
            await _sim_intermediate_text(cp, "Step 3")
            await _sim_tool(cp, "c3", "edit_file")

            await _sim_final(cp)

            assert _group_sizes(cp) == [1, 1, 1]
            assert _intermediate_texts(cp) == ["Step 1", "Step 2", "Step 3"]

    @pytest.mark.asyncio
    async def test_late_tool_event_after_group_boundary_routes_to_owning_group(self) -> None:
        """A result arriving after a new group opened completes its own call.

        Offering it to the current group first would silently drop it there,
        leaving the old call rendered as running forever.
        """
        async with _PanelApp().run_test() as pilot:
            cp = pilot.app.query_one(ChatPanel)
            await cp.add_user_message("go")

            await cp.add_tool_start("old", "read_file", "filesystem.read")
            cp._end_tool_group()
            await cp.add_tool_start("current", "grep", "search")
            groups = list(cp.query(ToolGroup))
            assert len(groups) == 2
            old_group, current_group = groups

            await cp.add_tool_result("old", "read_file", "late result", 10)
            await pilot.pause()

            assert old_group._tool_records["old"].status != "running"
            assert old_group._tool_records["old"].result == "late result"
            assert "old" not in current_group._tool_records
            assert current_group._tool_records["current"].status == "running"


# ---------------------------------------------------------------------------
# Replay tests — verify same grouping from serialized session messages
# ---------------------------------------------------------------------------


class TestReplayCoalescing:
    """Test that replay_history produces the same coalesced groups as live."""

    @pytest.mark.asyncio
    async def test_batch_merge_stops_at_user_message_boundary(self) -> None:
        """A repeated batch id must not merge tool groups across a user turn."""
        messages = [
            _user_msg("first"),
            _asst_msg([_fc("c1", "tool_a")], batch_id=1),
            _tool_msg([_fr("c1")]),
            _user_msg("second"),
            _asst_msg([_fc("c2", "tool_b")], batch_id=1),
            _tool_msg([_fr("c2")]),
        ]

        result = await _run_replay(messages)

        assert result["user_count"] == 2
        assert result["tool_group_sizes"] == [1, 1]

    @pytest.mark.asyncio
    async def test_replay_consecutive_singles_merge(self) -> None:
        """3 consecutive single-tool batches → 1 ToolGroup in replay."""
        messages = [
            _user_msg("go"),
            _asst_msg([_fc("c1", "read_file")], batch_id=1),
            _tool_msg([_fr("c1")]),
            _asst_msg([_fc("c2", "grep")], batch_id=2),
            _tool_msg([_fr("c2")]),
            _asst_msg([_fc("c3", "edit_file")], batch_id=3),
            _tool_msg([_fr("c3")]),
            _asst_msg([_text("Done.")]),
        ]
        result = await _run_replay(messages)
        assert result["tool_group_sizes"] == [3]

    @pytest.mark.asyncio
    async def test_replay_parallel_merges_with_neighbors(self) -> None:
        """Single → parallel(3) → single replays as one merged group (no text)."""
        messages = [
            _user_msg("go"),
            _asst_msg([_fc("c1", "read_file")], batch_id=1),
            _tool_msg([_fr("c1")]),
            _asst_msg([_fc("c2", "glob"), _fc("c3", "glob"), _fc("c4", "glob")], batch_id=2),
            _tool_msg([_fr("c2"), _fr("c3"), _fr("c4")]),
            _asst_msg([_fc("c5", "edit_file")], batch_id=3),
            _tool_msg([_fr("c5")]),
            _asst_msg([_text("Done.")]),
        ]
        result = await _run_replay(messages)
        assert result["tool_group_sizes"] == [5]

    @pytest.mark.asyncio
    async def test_replay_intermediate_text_breaks_merge(self) -> None:
        """Intermediate text between singles prevents merging in replay."""
        messages = [
            _user_msg("go"),
            _asst_msg([_fc("c1", "read_file")], batch_id=1),
            _tool_msg([_fr("c1")]),
            # Message with text + tool call → text splits as intermediate
            _asst_msg([_text("Let me search"), _fc("c2", "grep")], batch_id=2),
            _tool_msg([_fr("c2")]),
            _asst_msg([_fc("c3", "edit_file")], batch_id=3),
            _tool_msg([_fr("c3")]),
            _asst_msg([_text("Done.")]),
        ]
        result = await _run_replay(messages)
        # read_file alone, then grep+edit_file merged (text breaks before grep)
        assert result["tool_group_sizes"] == [1, 2]
        assert result["intermediate_count"] >= 1

    @pytest.mark.asyncio
    async def test_replay_single_after_parallel_keeps_one_group(self) -> None:
        """Singles following a parallel group keep merging into it."""
        messages = [
            _user_msg("go"),
            _asst_msg([_fc("c1", "glob"), _fc("c2", "glob")], batch_id=1),
            _tool_msg([_fr("c1"), _fr("c2")]),
            _asst_msg([_fc("c3", "read_file")], batch_id=2),
            _tool_msg([_fr("c3")]),
            _asst_msg([_fc("c4", "edit_file")], batch_id=3),
            _tool_msg([_fr("c4")]),
            _asst_msg([_text("Done.")]),
        ]
        result = await _run_replay(messages)
        assert result["tool_group_sizes"] == [4]

    @pytest.mark.asyncio
    async def test_replay_complex_mixed_scenario(self) -> None:
        """Full complex scenario in replay matches the expected grouping.

        Sequence:
            parallel(3), single, single, parallel(2),
            text+single, single,
            parallel(3), single, single, single

        Only the embedded text in batch 5 breaks the chain.
        Expected groups: [7, 8]
        """
        messages = [
            _user_msg("go"),
            # parallel(3)
            _asst_msg([_fc("c01", "glob"), _fc("c02", "glob"), _fc("c03", "glob")], batch_id=1),
            _tool_msg([_fr("c01"), _fr("c02"), _fr("c03")]),
            # single
            _asst_msg([_fc("c04", "read_file")], batch_id=2),
            _tool_msg([_fr("c04")]),
            # single
            _asst_msg([_fc("c05", "grep")], batch_id=3),
            _tool_msg([_fr("c05")]),
            # parallel(2)
            _asst_msg([_fc("c06", "glob"), _fc("c07", "glob")], batch_id=4),
            _tool_msg([_fr("c06"), _fr("c07")]),
            # text + single
            _asst_msg([_text("Analyzing results"), _fc("c08", "read_file")], batch_id=5),
            _tool_msg([_fr("c08")]),
            # single
            _asst_msg([_fc("c09", "edit_file")], batch_id=6),
            _tool_msg([_fr("c09")]),
            # parallel(3)
            _asst_msg([_fc("c10", "glob"), _fc("c11", "glob"), _fc("c12", "glob")], batch_id=7),
            _tool_msg([_fr("c10"), _fr("c11"), _fr("c12")]),
            # single
            _asst_msg([_fc("c13", "read_file")], batch_id=8),
            _tool_msg([_fr("c13")]),
            # single
            _asst_msg([_fc("c14", "edit_file")], batch_id=9),
            _tool_msg([_fr("c14")]),
            # single
            _asst_msg([_fc("c15", "write_file")], batch_id=10),
            _tool_msg([_fr("c15")]),
            # final
            _asst_msg([_text("All done.")]),
        ]
        result = await _run_replay(messages)
        assert result["tool_group_sizes"] == [7, 8]

    @pytest.mark.asyncio
    async def test_replay_all_singles_merge(self) -> None:
        """5 consecutive singles with distinct batch_ids → 1 ToolGroup."""
        messages = [
            _user_msg("go"),
            _asst_msg([_fc("c1", "tool_a")], batch_id=1),
            _tool_msg([_fr("c1")]),
            _asst_msg([_fc("c2", "tool_b")], batch_id=2),
            _tool_msg([_fr("c2")]),
            _asst_msg([_fc("c3", "tool_c")], batch_id=3),
            _tool_msg([_fr("c3")]),
            _asst_msg([_fc("c4", "tool_d")], batch_id=4),
            _tool_msg([_fr("c4")]),
            _asst_msg([_fc("c5", "tool_e")], batch_id=5),
            _tool_msg([_fr("c5")]),
            _asst_msg([_text("Done.")]),
        ]
        result = await _run_replay(messages)
        assert result["tool_group_sizes"] == [5]

    @pytest.mark.asyncio
    async def test_replay_intermediate_text_not_duplicated(self) -> None:
        """Intermediate text must appear exactly once on replay.

        Regression test: when a tool-only batch (empty intermediate) precedes
        a text+tools batch, ``persist_intermediate_texts`` could mis-attribute
        the text to the earlier tool-only message.  On replay the text would
        appear from both the ``_intermediate_text`` metadata AND the embedded
        message contents — rendering it twice.

        The fix ensures empty batch boundaries are included in the texts list
        so sequential iteration stays aligned with messages.
        """
        # Scenario: glob (tool-only) → "先看前 3 个文件。" + 3x read_file
        # The text is embedded in the read_file message's contents.
        # _intermediate_text metadata should NOT be set on the glob message.
        messages = [
            _user_msg("test"),
            # Batch 1: tool-only (glob) — no intermediate text
            _asst_msg([_fc("c1", "glob")], batch_id=1),
            _tool_msg([_fr("c1", "file1.py\nfile2.py")]),
            # Batch 2: text + 3 parallel reads — text embedded in contents
            _asst_msg(
                [_text("先看前 3 个文件。"), _fc("c2", "read_file"), _fc("c3", "read_file"), _fc("c4", "read_file")],
                batch_id=2,
            ),
            _tool_msg([_fr("c2", "code1"), _fr("c3", "code2"), _fr("c4", "code3")]),
            _asst_msg([_text("Done reading.")]),
        ]
        result = await _run_replay(messages)
        # Intermediate text should appear exactly once, not twice
        assert result["intermediate_count"] == 1
        assert result["intermediate_texts"] == ["先看前 3 个文件。"]

    @pytest.mark.asyncio
    async def test_replay_misattributed_intermediate_text(self) -> None:
        """_intermediate_text on wrong message must not cause duplication.

        If ``persist_intermediate_texts`` incorrectly sets ``_intermediate_text``
        on a tool-only message while the text is also in a later message's
        contents, replay should still show the text only once.

        This tests the replay merge logic's resilience — even with the
        mis-attributed metadata present.
        """
        # Simulate the buggy persisted state: _intermediate_text wrongly on glob
        messages = [
            _user_msg("test"),
            _asst_msg([_fc("c1", "glob")], batch_id=1, intermediate_text="先看前 3 个文件。"),
            _tool_msg([_fr("c1", "file1.py")]),
            _asst_msg(
                [_text("先看前 3 个文件。"), _fc("c2", "read_file"), _fc("c3", "read_file")],
                batch_id=2,
            ),
            _tool_msg([_fr("c2", "code1"), _fr("c3", "code2")]),
            _asst_msg([_text("Done.")]),
        ]
        result = await _run_replay(messages)
        assert result["intermediate_count"] == 1
        assert result["intermediate_texts"] == ["先看前 3 个文件。"]

    @pytest.mark.asyncio
    async def test_replay_user_message_breaks_coalescing(self) -> None:
        """User messages (injection) should break the tool group chain."""
        messages = [
            _user_msg("go"),
            _asst_msg([_fc("c1", "tool_a")], batch_id=1),
            _tool_msg([_fr("c1")]),
            _asst_msg([_fc("c2", "tool_b")], batch_id=2),
            _tool_msg([_fr("c2")]),
            _user_msg("interrupt"),
            _asst_msg([_fc("c3", "tool_c")], batch_id=3),
            _tool_msg([_fr("c3")]),
            _asst_msg([_fc("c4", "tool_d")], batch_id=4),
            _tool_msg([_fr("c4")]),
            _asst_msg([_text("Done.")]),
        ]
        result = await _run_replay(messages)
        # User message boundary prevents merge across it
        assert result["tool_group_sizes"] == [2, 2]

    @pytest.mark.asyncio
    async def test_replay_duplicate_call_ids_render_separate_slots(self) -> None:
        """Duplicate call_ids across retries must each get their own logical slot.

        Regression — some LLMs (MiniMax on OpenAI-compatible endpoints) reuse
        the same ``call_id`` when the agent retries the same tool after an
        approval rejection.  When the replay coalescer merges three such
        consecutive single-tool batches into one ToolGroup, keying logical
        tool state by call_id alone overwrites entries and leaves ``_done``
        exceeding the invocation count — the title renders "3/2" with three
        widgets on screen but only two stored slots.

        Matches session ``592e0190bdce`` (run pytest → reject → reject → approve).
        """
        dup = "call_function_k4gc5yl9j0yc_1"
        uniq = "call_function_c6tqmgyriy8i_1"
        messages = [
            _user_msg("uv run pytest"),
            # First attempt: rejected.
            _asst_msg([_fc(dup, "zsh")], batch_id=1),
            _tool_msg([_fr(dup, "Error: Tool execution was rejected by user.")]),
            # Second attempt: LLM reuses the SAME call_id — rejected again.
            _asst_msg([_fc(dup, "zsh")], batch_id=2),
            _tool_msg([_fr(dup, "Error: Tool execution was rejected by user.")]),
            # Third attempt with a fresh call_id — approved, pytest runs.
            _asst_msg([_fc(uniq, "zsh")], batch_id=3),
            _tool_msg([_fr(uniq, "============================= test session starts")]),
            _asst_msg([_text("全部通过。")]),
        ]
        result = await _run_replay(messages)

        # All three invocations merged into one ToolGroup (no intermediate text).
        assert result["tool_group_count"] == 1
        # Each invocation gets its own logical slot — without the fix the
        # duplicated call_id would collapse to 2 slots.
        assert result["tool_group_sizes"] == [3]
        # Done count matches slot count — would be 3/2 without the fix.
        assert result["tool_group_done"] == [3]
        # Results are paired in each adjacent assistant/tool-result block — the
        # third widget keeps the pytest output, not an overwritten rejection.
        results = result["tool_group_results"][0]
        assert results[0].startswith("Error:")
        assert results[1].startswith("Error:")
        assert "test session starts" in results[2]

    @pytest.mark.asyncio
    async def test_replay_parallel_results_out_of_order(self) -> None:
        """Parallel function_results persisted out of call order still pair correctly.

        Inside the adjacent tool-result block, pairing walks local
        per-``call_id`` lists rather than a single FIFO queue. A tool message
        whose ``function_result`` entries are reordered relative to the
        preceding assistant message's ``function_call`` entries still assigns
        each result to the right fc.
        """
        messages = [
            _user_msg("go"),
            _asst_msg([_fc("a1", "glob"), _fc("a2", "glob"), _fc("a3", "glob")], batch_id=1),
            # Results out of call order
            _tool_msg([_fr("a3", "res_3"), _fr("a1", "res_1"), _fr("a2", "res_2")]),
            _asst_msg([_text("done")]),
        ]
        result = await _run_replay(messages)
        assert result["tool_group_count"] == 1
        assert result["tool_group_sizes"] == [3]
        # Local per-call_id pairing: each fc gets ITS result regardless of fr order.
        results = result["tool_group_results"][0]
        assert results == ["res_1", "res_2", "res_3"]

    @pytest.mark.asyncio
    async def test_replay_missing_function_result_leaves_empty(self) -> None:
        """An fc without a local paired fr gets empty output and no spill."""
        messages = [
            _user_msg("go"),
            _asst_msg([_fc("c1", "tool_a"), _fc("c2", "tool_b")], batch_id=1),
            # Only c1 has a result — c2's is missing
            _tool_msg([_fr("c1", "got a")]),
            _asst_msg([_text("done")]),
        ]
        result = await _run_replay(messages)
        assert result["tool_group_sizes"] == [2]
        results = result["tool_group_results"][0]
        assert results == ["got a", ""]

    @pytest.mark.asyncio
    async def test_replay_duplicate_call_id_with_distinct_results(self) -> None:
        """Duplicate call_ids pair inside their adjacent local blocks."""
        dup = "x1"
        messages = [
            _user_msg("go"),
            _asst_msg([_fc(dup, "zsh")], batch_id=1),
            _tool_msg([_fr(dup, "first result")]),
            _asst_msg([_fc(dup, "zsh")], batch_id=2),
            _tool_msg([_fr(dup, "second result")]),
            _asst_msg([_text("done")]),
        ]
        result = await _run_replay(messages)
        assert result["tool_group_sizes"] == [2]
        # Not collapsed to a single last-write-wins result.
        results = result["tool_group_results"][0]
        assert results == ["first result", "second result"]

    @pytest.mark.asyncio
    async def test_replay_reused_call_id_does_not_borrow_result_from_previous_block(self) -> None:
        """A later reused call_id must not consume an extra result from an older block."""
        dup = "reuse"
        messages = [
            _user_msg("go"),
            _asst_msg([_fc(dup, "zsh")], batch_id=1),
            _tool_msg([_fr(dup, "first result"), _fr(dup, "extra old result")]),
            _asst_msg([_text("boundary")]),
            _asst_msg([_fc(dup, "zsh")], batch_id=2),
            _asst_msg([_text("done")]),
        ]
        result = await _run_replay(messages)

        results = [tool_result for group in result["tool_group_results"] for tool_result in group]
        assert results == ["first result", ""]

    @pytest.mark.asyncio
    async def test_replay_orphan_tool_result_does_not_pair_globally(self) -> None:
        """A tool result outside an adjacent result block is ignored for pairing."""
        messages = [
            _tool_msg([_fr("orphan", "old orphan")]),
            _user_msg("go"),
            _asst_msg([_fc("orphan", "zsh")], batch_id=1),
            _asst_msg([_text("done")]),
        ]
        result = await _run_replay(messages)

        assert result["tool_group_results"][0] == [""]

    @pytest.mark.asyncio
    async def test_replay_provider_limited_history_keeps_pairing_local(self) -> None:
        """Provider-limited history with only an orphan result does not satisfy a later call."""
        messages = [
            _tool_msg([_fr("limited", "stale result")]),
            _asst_msg([_fc("limited", "zsh")], batch_id=1),
            _asst_msg([_text("done")]),
        ]
        result = await _run_replay(messages)

        assert result["tool_group_results"][0] == [""]

    @pytest.mark.asyncio
    async def test_replay_same_tool_name_keeps_distinct_approval_statuses(self) -> None:
        """Approval replay is per invocation, not last status by tool name."""
        messages = [
            _user_msg("count lines"),
            _asst_msg(
                [_fc("c1", "zsh")],
                batch_id=1,
                approval={"tool_name": "zsh", "status": "user_approved", "request_id": "req-1"},
            ),
            _tool_msg([_fr("c1", "192268 total")]),
            _asst_msg(
                [_fc("c2", "zsh")],
                batch_id=2,
                approval={"tool_name": "zsh", "status": "user_rejected", "request_id": "req-2"},
            ),
            _tool_msg([_fr("c2", "Error: Tool execution was rejected by user.")]),
            _asst_msg([_text("done")]),
        ]
        result = await _run_replay(messages)

        assert result["tool_group_sizes"] == [2]
        assert result["tool_group_rejected"][0] == [False, True]


# ---------------------------------------------------------------------------
# Replay widget helper
# ---------------------------------------------------------------------------


async def _run_replay(raw_messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Run replay_history in a headless Textual app and inspect widget tree."""

    class ReplayApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with ReplayApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(raw_messages)
        await pilot.pause()

        user_msgs = cp.query(UserMessageWidget)
        tool_groups = cp.query(ToolGroup)
        agent_msgs = cp.query(AgentMessageWidget)

        intermediates = [m for m in agent_msgs if m._is_intermediate]
        return {
            "user_count": len(user_msgs),
            "tool_group_count": len(tool_groups),
            "tool_group_sizes": [len(tg._tool_records) for tg in tool_groups],
            "tool_group_done": [tg._done for tg in tool_groups],
            "tool_group_results": [[record.result for record in tg._tool_records.values()] for tg in tool_groups],
            "tool_group_approvals": [[record.approval for record in tg._tool_records.values()] for tg in tool_groups],
            "tool_group_rejected": [
                [record.approval == "user_rejected" for record in tg._tool_records.values()] for tg in tool_groups
            ],
            "agent_message_count": len(agent_msgs),
            "intermediate_count": len(intermediates),
            "intermediate_texts": [m.text for m in intermediates],
        }
