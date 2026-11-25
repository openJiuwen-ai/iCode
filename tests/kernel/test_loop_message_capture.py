# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for LoopRecorder and _merge_loop_messages.

Verifies that:
- LoopRecorder correctly captures tool loop messages
- _merge_loop_messages inserts recovered messages at the correct position
- Normal completion (no lost messages) is a no-op
- Edge cases: single iteration, no tool calls, empty state
- Driven through the production loop stack: pre-call snapshots, checkpoint
  hashing, service-mode capture, journal commit points and cancellation slots
"""

from __future__ import annotations

import asyncio
import logging
from threading import Event as ThreadEvent
from typing import TYPE_CHECKING, Any

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.recovery import RecoveryPersistOutcome
from chrys.foundation.retry import HistorySnapshot, StreamRetryLoop
from chrys.foundation.tool_invocation_order import TOOL_INVOCATION_ORDER_KEY
from chrys.foundation.tool_result_metadata import (
    HOOK_RESULT_APPLIED_KEY,
    HOOK_RESULT_PENDING_KEY,
    TOOL_INTERRUPTED_METADATA_KEY,
    TOOL_POST_PROCESSING_INTERRUPTED_METADATA_KEY,
    TOOL_RESULT_METADATA_KEY,
)
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.foundation.trajectory_timing import TRAJECTORY_TIMING_KEY
from chrys.foundation.util.sub_agent_context import SUB_AGENT_RESULT_COMMIT_CALLBACK_KEY
from chrys.kernel import Agent, ChatResponse, Content, FunctionTool, LoopRecorder, Message, tool
from chrys.kernel.middleware import ChatMiddlewareLayer, FunctionInvocationContext, FunctionMiddleware
from chrys.kernel.tools import SyncToolCancelledAfterCompletion
from chrys.kernel.types import ResponseStream
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.session.history import SessionHistoryManager
from chrys.service.state.serializers import serialized_message_payload
from tests.kernel._fakes import (
    _call_response,
    _call_update,
    _make_tool,
    _ScriptedClient,
    _stack,
    _text_response,
    _user,
)
from tests.support.transcript_invariants import InvariantCheckedToolLoopLayer
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@tool
def loop_capture_test_tool() -> str:
    return "tool result"


def _stamped_call(ordinal: int, *, call_id: str | None = None) -> Content:
    call = Content.from_function_call(call_id if call_id is not None else f"call-{ordinal}", "echo", arguments={})
    call.additional_properties[TOOL_INVOCATION_ORDER_KEY] = ordinal
    return call


# ---------------------------------------------------------------------------
# LoopRecorder unit tests
# ---------------------------------------------------------------------------


class TestLoopRecorder:
    """Test the loop-fed LoopRecorder (#405 capture semantics)."""

    def test_initial_state(self):
        capture = LoopRecorder()
        assert capture.loop_messages is None

    def test_single_iteration_no_loop_messages(self):
        """Single LLM call (no tool loop): loop_messages is None."""
        capture = LoopRecorder()
        # Simulate first (and only) LLM call with 5 initial messages
        capture._initial_count = 5
        capture._captured = [Message("system", ["sys"]), Message("user", ["hi"])]  # Only initial
        assert capture.loop_messages is None

    def test_multi_iteration_captures_delta(self):
        """Multiple iterations: loop_messages returns the delta."""
        capture = LoopRecorder()

        # Simulate: initial messages = [sys, history, user] (count=3)
        sys_msg = Message("system", ["instructions"])
        hist_msg = Message("assistant", ["previous response"])
        user_msg = Message("user", ["do something"])
        initial = [sys_msg, hist_msg, user_msg]

        capture._initial_count = len(initial)

        # After iteration 0: prepped_messages extended with assistant+tool
        iter0_assistant = Message("assistant", ["tool call 0"])
        iter0_tool = Message("tool", ["result 0"])

        # After iteration 1: prepped_messages extended again
        iter1_assistant = Message("assistant", ["tool call 1"])
        iter1_tool = Message("tool", ["result 1"])

        # Captured at iteration 2 (before 3rd LLM call)
        capture._captured = [*initial, iter0_assistant, iter0_tool, iter1_assistant, iter1_tool]

        loop_msgs = capture.loop_messages
        assert loop_msgs is not None
        assert len(loop_msgs) == 4
        assert loop_msgs[0] is iter0_assistant
        assert loop_msgs[1] is iter0_tool
        assert loop_msgs[2] is iter1_assistant
        assert loop_msgs[3] is iter1_tool

    def test_service_side_loop_captures_replaced_continuation_messages(self):
        """Responses-style service continuation can replace the growing local prepped list."""
        capture = LoopRecorder(capture_service_loop_messages=True)
        assistant_call = Message("assistant", [Content.from_function_call("call-1", "sleep", arguments={})])
        tool_result = Message("tool", [Content.from_function_result("call-1", result="slept")])

        capture._initial_count = 1
        capture._captured = [tool_result]
        capture._service_loop_messages = [assistant_call, tool_result]

        assert capture.loop_messages == [assistant_call, tool_result]

    @pytest.mark.asyncio
    async def test_conversation_id_alone_does_not_enable_service_capture(self):
        """Non-Responses providers may populate conversation_id; that should not switch capture mode."""
        capture = LoopRecorder()
        assistant_call = Message("assistant", [Content.from_function_call("call-1", "sleep", arguments={})])

        await capture.record_pre_call([Message("user", ["hi"])])
        capture.record_response(ChatResponse(messages=[assistant_call], conversation_id="provider-conversation"))

        assert capture.loop_messages is None
        assert capture._service_loop_messages == []

    @pytest.mark.asyncio
    async def test_checkpoint_callback_is_suppressed_for_unchanged_prefix(self):
        calls = 0

        async def on_checkpoint() -> None:
            nonlocal calls
            calls += 1

        capture = LoopRecorder(on_checkpoint=on_checkpoint)
        messages = [Message("user", ["hi"])]

        await capture.record_pre_call(messages)
        await capture.record_pre_call(messages)

        assert calls == 1

    @pytest.mark.asyncio
    async def test_checkpoint_callback_runs_when_prefix_content_changes(self):
        calls = 0

        async def on_checkpoint() -> None:
            nonlocal calls
            calls += 1

        capture = LoopRecorder(on_checkpoint=on_checkpoint)

        await capture.record_pre_call([Message("user", ["hi"])])
        await capture.record_pre_call([Message("user", ["different"])])

        assert calls == 2

    @pytest.mark.asyncio
    async def test_checkpoint_suppression_resets_between_runs(self):
        calls = 0

        async def on_checkpoint() -> None:
            nonlocal calls
            calls += 1

        capture = LoopRecorder(on_checkpoint=on_checkpoint)
        messages = [Message("user", ["hi"])]

        await capture.record_pre_call(messages)
        capture.reset()
        await capture.record_pre_call(messages)

        assert calls == 2

    @pytest.mark.asyncio
    async def test_service_capture_uses_explicit_mode_not_conversation_id(self):
        capture = LoopRecorder(capture_service_loop_messages=True)
        assistant_call = Message("assistant", [Content.from_function_call("call-1", "sleep", arguments={})])

        await capture.record_pre_call([Message("user", ["hi"])])
        capture.record_response(ChatResponse(messages=[assistant_call]))

        assert capture.loop_messages == [assistant_call]

    @pytest.mark.asyncio
    async def test_streaming_service_capture_records_compact_continuation_messages(self):
        """Streaming Responses continuations finalize through ResponseStream result hooks."""
        capture = LoopRecorder(capture_service_loop_messages=True)
        client = MockChatClient(
            responses=[
                MockResponse(
                    tool_calls=[("loop_capture_test_tool", "call-1", {})],
                    conversation_id="resp_tool_call",
                    chunk_delay=0,
                ),
                MockResponse(text="done", conversation_id="resp_done", chunk_delay=0),
            ]
        )

        async with Agent(
            client=client,
            name="capture-test",
            instructions="Use the tool, then finish.",
            tools=[loop_capture_test_tool],
        ) as agent:
            stream = agent.run(
                "hi",
                stream=True,
                options={"store": True},
                client_kwargs={"loop_recorder": capture},
            )
            async for _update in stream:
                pass
            final = await stream.get_final_response()

        assert final.text == "done"
        assert [msg.role for msg in client.call_history[1][0]] == ["tool"]
        loop_messages = capture.loop_messages
        assert loop_messages is not None
        assert [msg.role for msg in loop_messages] == ["assistant", "tool"]
        assert loop_messages[0].contents[0].call_id == "call-1"
        assert loop_messages[1].contents[0].call_id == "call-1"

    def test_reset_clears_state(self):
        capture = LoopRecorder()
        capture._initial_count = 5
        capture._captured = [Message("system", ["x"])] * 10
        capture._service_loop_messages = [Message("assistant", ["service"])]
        assert capture.loop_messages is not None

        capture.reset()
        assert capture.loop_messages is None
        assert capture._initial_count is None
        assert capture._captured is None
        assert capture._service_loop_messages == []

    def test_pending_journal_projects_only_answered_ordinals_in_slot_order(self) -> None:
        capture = LoopRecorder()
        calls = [_stamped_call(index) for index in range(5)]
        assistant = Message("assistant", ["working", *calls])
        commits = capture.stage_exchange([assistant], calls, result_carrier_item_id="a" * 32)

        for index in (0, 2, 4):
            commits[index].commit_final(Content.from_function_result(calls[index].call_id, result=f"result-{index}"))

        projected = capture.loop_messages
        assert projected is not None
        assert [message.role for message in projected] == ["assistant", "tool"]
        assert projected[0].text == "working"
        assert [content for content in projected[0].contents if content.type == "function_call"] == [
            calls[0],
            calls[2],
            calls[4],
        ]
        assert [content.result for content in projected[1].contents] == ["result-0", "result-2", "result-4"]
        assert projected[1].additional_properties[ANALYTICS_ITEM_ID_KEY] == "a" * 32
        assert capture.committed_count == 3

    def test_sealed_journal_reuses_canonical_result_content_without_duplicates(self) -> None:
        capture = LoopRecorder()
        calls = [_stamped_call(0), _stamped_call(1)]
        assistant = Message("assistant", calls)
        commits = capture.stage_exchange([assistant], calls, result_carrier_item_id="a" * 32)
        results = [
            Content.from_function_result(calls[0].call_id, result="first"),
            Content.from_function_result(calls[1].call_id, result="second"),
        ]
        for commit, result in zip(commits, results, strict=True):
            commit.commit_final(result)
        canonical_tool = Message("tool", results)
        capture.seal_exchange(canonical_tool)
        capture._initial_count = 0
        capture._captured = [assistant, canonical_tool]

        projected = capture.loop_messages
        assert projected == [assistant, canonical_tool]
        assert projected[1] is canonical_tool
        assert projected[1].contents == results
        assert capture.committed_count == 2

    def test_falsy_id_slots_are_associated_by_ordinal_not_position(self) -> None:
        capture = LoopRecorder(capture_service_loop_messages=True)
        first = _stamped_call(0, call_id="")
        second = _stamped_call(1, call_id="")
        assistant = Message("assistant", [first, second])
        capture.record_response(ChatResponse(messages=[assistant]))
        commits = capture.stage_exchange([assistant], [first, second], result_carrier_item_id="a" * 32)

        commits[1].commit_final(Content.from_function_result("", result="second-only"))

        projected = capture.loop_messages
        assert projected is not None
        assert projected[0].contents == [second]
        assert [content.result for content in projected[1].contents] == ["second-only"]

    def test_snapshot_restore_preserves_commits_and_reset_starts_a_new_pass(self) -> None:
        capture = LoopRecorder()
        snapshot = capture.snapshot()
        unanswered = _stamped_call(0)
        capture.stage_exchange([Message("assistant", [unanswered])], [unanswered], result_carrier_item_id="a" * 32)

        capture.restore(snapshot)
        assert capture.loop_messages is None
        assert capture.committed_count == 0

        answered = _stamped_call(1)
        commit = capture.stage_exchange(
            [Message("assistant", [answered])], [answered], result_carrier_item_id="a" * 32
        )[0]
        result = Content.from_function_result(answered.call_id, result="durable")
        commit.commit_final(result)
        capture.restore(snapshot)

        assert capture.committed_count == 1
        assert capture.loop_messages is not None
        capture.seal_exchange(Message("tool", [result]))
        capture.restore(snapshot)
        assert capture.committed_count == 1

        capture.reset()
        assert capture.committed_count == 0
        assert capture.loop_messages is None

    def test_interrupted_fills_are_not_commits_and_restore_allows_redispatch(self) -> None:
        capture = LoopRecorder()
        snapshot = capture.snapshot()
        call = _stamped_call(0)
        commit = capture.stage_exchange([Message("assistant", [call])], [call], result_carrier_item_id="a" * 32)[0]
        commit.commit_interrupted(call, None)

        assert capture.committed_count == 0
        projected = capture.loop_messages
        assert projected is not None
        marker = projected[-1].contents[0]
        assert marker.additional_properties[TOOL_RESULT_METADATA_KEY]["interrupted"] is True

        capture.restore(snapshot)
        assert capture.loop_messages is None
        assert capture.committed_count == 0

    def test_sealed_interrupted_fill_accepts_late_audit_metadata(self) -> None:
        capture = LoopRecorder()
        call = _stamped_call(0)
        commit = capture.stage_exchange([Message("assistant", [call])], [call], result_carrier_item_id="a" * 32)[0]
        commit.commit_interrupted(call, None, {"sub_agent_invocation_id": "inv-1"})
        projected = capture.loop_messages
        assert projected is not None
        interrupted = projected[-1].contents[0]
        capture.seal_exchange(Message("tool", [interrupted]))

        commit.commit_interrupted(
            call,
            None,
            {
                "sub_agent_invocation_id": "inv-1",
                "sub_agent_log_file": "child.json",
                "sub_agent_audit_complete": True,
            },
        )

        metadata = interrupted.additional_properties[TOOL_RESULT_METADATA_KEY]
        assert metadata["sub_agent_invocation_id"] == "inv-1"
        assert metadata["sub_agent_log_file"] == "child.json"
        assert metadata["sub_agent_audit_complete"] is True
        assert metadata["interrupted"] is True

    @pytest.mark.asyncio
    async def test_barrier_retries_non_persisted_outcomes_once_then_degrades(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        outcomes = [RecoveryPersistOutcome.FAILED, RecoveryPersistOutcome.NOTHING_TO_PERSIST]
        barrier_calls = 0
        checkpoint_calls = 0

        async def barrier() -> RecoveryPersistOutcome:
            nonlocal barrier_calls
            outcome = outcomes[barrier_calls]
            barrier_calls += 1
            return outcome

        async def checkpoint() -> None:
            nonlocal checkpoint_calls
            checkpoint_calls += 1

        capture = LoopRecorder(on_pre_wire_barrier=barrier, on_result_checkpoint=checkpoint)
        call = _stamped_call(0)
        commit = capture.stage_exchange([Message("assistant", [call])], [call], result_carrier_item_id="a" * 32)[0]
        commit.commit_final(Content.from_function_result(call.call_id, result="done"))
        await asyncio.sleep(0)

        with caplog.at_level(logging.ERROR, logger="chrys.kernel.loop"):
            await capture.record_pre_call([Message("user", ["continue"])])
            await capture.record_pre_call([Message("user", ["continue again"])])

        assert barrier_calls == 2
        assert checkpoint_calls >= 1
        assert (
            caplog.messages.count(
                "Recovery sidecar persistence failed twice after committed tool work; "
                "continuing with the in-memory journal."
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_persisted_barrier_replaces_the_pre_call_checkpoint(self) -> None:
        """A persisted barrier already holds the pre-call state, so no best-effort
        checkpoint rebuilds it; the prefix still counts as checkpointed."""
        outcomes = [
            RecoveryPersistOutcome.PERSISTED,
            RecoveryPersistOutcome.FAILED,
            RecoveryPersistOutcome.FAILED,
        ]
        barrier_calls = 0
        checkpoint_calls = 0

        async def barrier() -> RecoveryPersistOutcome:
            nonlocal barrier_calls
            outcome = outcomes[barrier_calls]
            barrier_calls += 1
            return outcome

        async def checkpoint() -> None:
            nonlocal checkpoint_calls
            checkpoint_calls += 1

        capture = LoopRecorder(on_pre_wire_barrier=barrier, on_result_checkpoint=checkpoint)
        call = _stamped_call(0)
        commit = capture.stage_exchange([Message("assistant", [call])], [call], result_carrier_item_id="a" * 32)[0]
        commit.commit_final(Content.from_function_result(call.call_id, result="done"))
        await wait_for(lambda: checkpoint_calls == 1, description="the slot-fill checkpoint kick ran")
        prefix = [Message("user", ["continue"])]

        await capture.record_pre_call(prefix)
        assert (barrier_calls, checkpoint_calls) == (1, 1)

        # Same request again, now with a barrier that fails twice and degrades:
        # the persisted barrier recorded this prefix, so it stays suppressed.
        await capture.record_pre_call(list(prefix))
        assert (barrier_calls, checkpoint_calls) == (3, 1)

        # A changed prefix under the degraded barrier checkpoints best-effort.
        await capture.record_pre_call([*prefix, Message("user", ["more"])])
        assert (barrier_calls, checkpoint_calls) == (3, 2)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome",
        [RecoveryPersistOutcome.NOTHING_TO_PERSIST, RecoveryPersistOutcome.UNCONFIGURED],
    )
    async def test_non_persisted_barrier_outcomes_keep_the_pre_call_checkpoint(
        self,
        outcome: RecoveryPersistOutcome,
    ) -> None:
        checkpoint_calls = 0

        async def barrier() -> RecoveryPersistOutcome:
            return outcome

        async def checkpoint() -> None:
            nonlocal checkpoint_calls
            checkpoint_calls += 1

        capture = LoopRecorder(on_pre_wire_barrier=barrier, on_result_checkpoint=checkpoint)
        call = _stamped_call(0)
        commit = capture.stage_exchange([Message("assistant", [call])], [call], result_carrier_item_id="a" * 32)[0]
        commit.commit_final(Content.from_function_result(call.call_id, result="done"))
        await wait_for(lambda: checkpoint_calls == 1, description="the slot-fill checkpoint kick ran")

        await capture.record_pre_call([Message("user", ["continue"])])

        assert checkpoint_calls == 2

    @pytest.mark.asyncio
    async def test_unconfigured_barrier_becomes_a_silent_permanent_noop(self) -> None:
        calls = 0

        async def barrier() -> RecoveryPersistOutcome:
            nonlocal calls
            calls += 1
            return RecoveryPersistOutcome.UNCONFIGURED

        capture = LoopRecorder(on_pre_wire_barrier=barrier)
        call = _stamped_call(0)
        commit = capture.stage_exchange([Message("assistant", [call])], [call], result_carrier_item_id="a" * 32)[0]
        commit.commit_final(Content.from_function_result(call.call_id, result="done"))

        await capture.record_pre_call([Message("user", ["one"])])
        await capture.record_pre_call([Message("user", ["two"])])

        assert calls == 1

    @pytest.mark.asyncio
    async def test_barrier_fault_keeps_narration_and_approval_metadata_aligned_to_answered_slot(self) -> None:
        async def barrier() -> RecoveryPersistOutcome:
            return RecoveryPersistOutcome.FAILED

        capture = LoopRecorder(on_pre_wire_barrier=barrier)
        first = _stamped_call(0)
        second = _stamped_call(1)
        assistant = Message("assistant", ["I need approval before the write.", first, second])
        commits = capture.stage_exchange([assistant], [first, second], result_carrier_item_id="a" * 32)
        approved_result = Content.from_function_result(second.call_id, result="approved result")
        approved_result.additional_properties[TOOL_RESULT_METADATA_KEY] = {
            "approval": "user_approved",
        }
        commits[1].commit_final(approved_result)

        await capture.record_pre_call([Message("user", ["continue"])])

        projected = capture.loop_messages
        assert projected is not None
        assert projected[0].text == "I need approval before the write."
        assert [content.call_id for content in projected[0].contents if content.type == "function_call"] == [
            second.call_id
        ]
        assert projected[1].contents == [approved_result]
        assert projected[1].contents[0].additional_properties[TOOL_RESULT_METADATA_KEY] == {
            "approval": "user_approved",
        }

    def test_stale_initial_count_causes_duplication(self):
        """Without reset between approval iterations, stale _initial_count
        causes LoopRecorder to include messages already stored by HistoryProvider.after_run,
        leading to duplicates in session state.
        """
        capture = LoopRecorder()

        # --- Run 1: LLM generates read_file, then write_file (needs approval) ---
        # Before LLM call 1: initial_count set
        run1_context_before_call1 = [
            Message("system", ["sys"]),
            Message("user", ["do work"]),
        ]
        capture._initial_count = len(run1_context_before_call1)  # 2

        # Before LLM call 2: captured includes read_file from call 1
        read_call = Message("assistant", ["read_file call"])
        read_result = Message("tool", ["read_file result"])
        run1_context_before_call2 = [*run1_context_before_call1, read_call, read_result]
        capture._captured = list(run1_context_before_call2)

        # Loop messages from run 1 correctly captures read_file pair
        assert capture.loop_messages == [read_call, read_result]

        # --- Without reset, start run 2 (re-submission with approval) ---
        # Run 2's context is different — includes write_file approval in input
        write_call = Message("assistant", ["write_file call"])
        write_approval = Message("user", ["approved"])
        write_result = Message("tool", ["write_file result"])
        # After write_file executes, LLM returns read_file_2
        read_call_2 = Message("assistant", ["read_file_2 call"])
        read_result_2 = Message("tool", ["read_file_2 result"])

        # Before LLM call 2 of run 2 (captured includes run 2's context)
        run2_context = [
            Message("system", ["sys"]),
            Message("user", ["do work"]),
            write_call,
            write_approval,
            write_result,
            read_call_2,
            read_result_2,
        ]
        # _initial_count is still 2 from run 1!
        capture._captured = list(run2_context)

        # BUG: loop_messages includes write_file messages that
        # HistoryProvider.after_run already stored — causes duplication
        stale_loop_msgs = capture.loop_messages
        assert stale_loop_msgs is not None
        assert len(stale_loop_msgs) == 5  # Too many! Includes write_file + approval + result
        assert write_call in stale_loop_msgs  # These would be duplicates

    def test_reset_between_approval_iterations_prevents_duplication(self):
        """With reset after each capture, the next run starts fresh and only
        captures its own intermediate messages.
        """
        capture = LoopRecorder()
        pending: list[Message] = []

        # --- Run 1: captures read_file ---
        run1_initial = [Message("system", ["sys"]), Message("user", ["do work"])]
        capture._initial_count = len(run1_initial)
        read_call = Message("assistant", ["read_file call"])
        read_result = Message("tool", ["read_file result"])
        capture._captured = [*run1_initial, read_call, read_result]

        loop_msgs = capture.loop_messages
        assert loop_msgs == [read_call, read_result]
        pending.extend(loop_msgs)

        # Reset after capture (the fix)
        capture.reset()
        assert capture.loop_messages is None

        # --- Run 2: fresh start, captures read_file_2 only ---
        write_call = Message("assistant", ["write_file call"])
        write_approval = Message("user", ["approved"])
        write_result = Message("tool", ["write_file result"])
        read_call_2 = Message("assistant", ["read_file_2 call"])
        read_result_2 = Message("tool", ["read_file_2 result"])

        run2_initial = [
            Message("system", ["sys"]),
            Message("user", ["do work"]),
            write_call,
            write_approval,
            write_result,
        ]
        capture._initial_count = len(run2_initial)  # Fresh count for run 2
        capture._captured = [*run2_initial, read_call_2, read_result_2]

        loop_msgs_2 = capture.loop_messages
        assert loop_msgs_2 == [read_call_2, read_result_2]  # Only run 2's intermediates
        assert write_call not in loop_msgs_2  # No duplicates
        pending.extend(loop_msgs_2)

        # Final pending has both runs' intermediates, no duplicates
        assert len(pending) == 4
        assert pending == [read_call, read_result, read_call_2, read_result_2]

    # -- LoopRecorder driven through the production loop stack --------------

    @pytest.mark.asyncio
    async def test_pre_call_snapshot_and_slice_semantics(self) -> None:
        recorder = LoopRecorder()
        first = [_user("a")]
        assert recorder.loop_messages is None
        await recorder.record_pre_call(first)
        assert recorder.initial_count == 1
        assert recorder.captured_count == 1
        assert recorder.loop_messages is None, "single iteration → no loop messages"
        grown = [*first, Message(role="assistant", contents=["x"]), Message(role="tool", contents=["y"])]
        await recorder.record_pre_call(grown)
        assert recorder.initial_count == 1, "initial count locks on first call"
        tail = recorder.loop_messages
        assert tail is not None
        assert [m.role for m in tail] == ["assistant", "tool"]
        assert tail[0] is grown[1], "snapshot preserves message object identity"

    @pytest.mark.asyncio
    async def test_checkpoint_fires_on_change_and_suppresses_on_identical_prefix(self) -> None:
        fired: list[int] = []

        async def on_checkpoint() -> None:
            fired.append(1)

        hashed: list[str] = []

        def hasher(message: Message) -> str:
            payload = f"hashed:{message.role}"
            hashed.append(payload)
            return payload

        recorder = LoopRecorder(on_checkpoint=on_checkpoint, message_hasher=hasher)
        messages = [_user("a")]
        await recorder.record_pre_call(messages)
        await recorder.record_pre_call(list(messages))
        assert len(fired) == 1, "identical prefix must suppress the second checkpoint"
        assert hashed, "the injected hasher must feed the suppression key"
        await recorder.record_pre_call([*messages, Message(role="assistant", contents=["x"])])
        assert len(fired) == 2

    @pytest.mark.asyncio
    async def test_checkpoint_hash_accepts_surrogates_with_production_message_hasher(self) -> None:
        fired: list[int] = []

        async def on_checkpoint() -> None:
            fired.append(1)

        recorder = LoopRecorder(on_checkpoint=on_checkpoint, message_hasher=serialized_message_payload)
        messages = [Message(role="tool", contents=["result with split pair \ud83c\udf0d and lone \ud83c"])]

        await recorder.record_pre_call(messages)
        await recorder.record_pre_call(list(messages))

        assert len(fired) == 1, "the unchanged production payload must still suppress duplicate checkpoints"

    @pytest.mark.asyncio
    async def test_checkpoint_hash_does_not_alias_surrogate_to_literal_escape(self) -> None:
        fired: list[int] = []

        async def on_checkpoint() -> None:
            fired.append(1)

        def text_hasher(message: Message) -> str:
            return message.contents[0].text or ""

        recorder = LoopRecorder(on_checkpoint=on_checkpoint, message_hasher=text_hasher)
        await recorder.record_pre_call([Message(role="tool", contents=["\ud83c"])])
        await recorder.record_pre_call([Message(role="tool", contents=[r"\ud83c"])])

        assert len(fired) == 2, "a surrogate and the literal escape that spells it must hash differently"

    @pytest.mark.asyncio
    async def test_service_mode_capture_paths(self) -> None:
        recorder = LoopRecorder(capture_service_loop_messages=True)
        await recorder.record_pre_call([_user("a")])
        call_msg = Message(
            role="assistant", contents=[Content.from_function_call(call_id="c1", name="t", arguments={})]
        )
        recorder.record_response(ChatResponse(messages=[call_msg]))
        # Seeded by the response capture; later pre-calls collect assistant/tool messages.
        tool_msg = Message(role="tool", contents=[Content.from_function_result(call_id="c1", result="r")])
        await recorder.record_pre_call([tool_msg])
        loop_messages = recorder.loop_messages
        assert loop_messages is not None
        assert loop_messages[0] is call_msg
        assert loop_messages[1] is tool_msg
        # Identity dedup: recording the same objects again must not duplicate.
        recorder.record_response(ChatResponse(messages=[call_msg]))
        await recorder.record_pre_call([tool_msg])
        assert len(recorder.loop_messages or []) == 2

    @pytest.mark.asyncio
    async def test_non_service_mode_ignores_record_response(self) -> None:
        recorder = LoopRecorder()
        call_msg = Message(
            role="assistant", contents=[Content.from_function_call(call_id="c1", name="t", arguments={})]
        )
        recorder.record_response(ChatResponse(messages=[call_msg]))
        await recorder.record_pre_call([_user()])
        assert recorder.loop_messages is None

    @pytest.mark.asyncio
    async def test_retry_snapshot_restores_every_mutable_recorder_field(self) -> None:
        recorder = LoopRecorder(capture_service_loop_messages=True)
        user = _user("start")
        await recorder.record_pre_call([user])
        call = Message(role="assistant", contents=[Content.from_function_call(call_id="c1", name="echo", arguments={})])
        recorder.record_response(ChatResponse(messages=[call]))
        snapshot = recorder.snapshot()

        await recorder.record_pre_call([user, call, Message(role="tool", contents=["failed"])])
        recorder.record_response(_call_response(("c2", "echo", {"text": "failed"})))
        recorder.restore(snapshot)

        assert recorder.initial_count == 1
        assert recorder.captured_count == 1
        assert recorder.loop_messages == [call]

    @pytest.mark.asyncio
    async def test_loop_feeds_recorder_via_client_kwargs(self) -> None:
        recorder = LoopRecorder()
        layer, wire = _stack([_call_response(("c1", "echo", {"text": "a"})), _text_response()])
        await layer.get_response(
            [_user()],
            options={"tools": [_make_tool()]},
            client_kwargs={"loop_recorder": recorder},
        )
        assert recorder.initial_count == 1
        tail = recorder.loop_messages
        assert tail is not None
        assert [m.role for m in tail] == ["assistant", "tool"]
        # The recorder key never reaches the wire.
        for call in wire.calls:
            assert "loop_recorder" not in call["kwargs"].get("client_kwargs", {})

    @pytest.mark.asyncio
    async def test_concurrent_runs_use_isolated_recorders(self) -> None:
        rec_a = LoopRecorder()
        rec_b = LoopRecorder()
        gate = asyncio.Barrier(2)

        @tool(name="sync")
        async def sync_tool(text: str) -> str:
            await asyncio.wait_for(gate.wait(), timeout=2)
            return text

        wire = _ScriptedClient(
            [
                _call_response(("c1", "sync", {"text": "a"})),
                _call_response(("c2", "sync", {"text": "b"})),
                _text_response("a-final"),
                _text_response("b-final"),
            ]
        )
        layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))
        run_a = layer.get_response([_user("a")], options={"tools": [sync_tool]}, client_kwargs={"loop_recorder": rec_a})
        run_b = layer.get_response(
            [_user("b"), _user("b2")], options={"tools": [sync_tool]}, client_kwargs={"loop_recorder": rec_b}
        )
        await asyncio.gather(run_a, run_b)
        assert rec_a.initial_count == 1
        assert rec_b.initial_count == 2
        assert len(rec_a.loop_messages or []) == 2
        assert len(rec_b.loop_messages or []) == 2

    @pytest.mark.asyncio
    async def test_concurrent_runs_on_shared_stack_strip_only_their_own_echoes(self) -> None:
        """Two simultaneous runs share one client stack; the echo memo is
        PER-RUN. A content object from run A's history echoed by the shared
        client is stripped in A (echo of A's conversation) but lands in B
        (fresh work there) — a stack-shared memo would strip B's copy too."""
        shared_x = Content.from_text("A-history")
        history_a = [_user("a"), Message("assistant", [shared_x]), _user("go")]
        history_b = [_user("b")]
        gate = asyncio.Barrier(2)

        @tool(name="sync")
        async def sync_tool(text: str) -> str:
            await asyncio.wait_for(gate.wait(), timeout=2)
            return text

        # Both second-round turns carry the SAME object from A's history, so
        # the pin holds regardless of which run receives which turn.
        wire = _ScriptedClient(
            [
                _call_response(("c1", "sync", {"text": "a"})),
                _call_response(("c2", "sync", {"text": "b"})),
                ChatResponse(messages=[Message("assistant", [shared_x, Content.from_text("fresh-1")])]),
                ChatResponse(messages=[Message("assistant", [shared_x, Content.from_text("fresh-2")])]),
            ]
        )
        layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))

        response_a, response_b = await asyncio.gather(
            layer.get_response(history_a, options={"tools": [sync_tool]}),
            layer.get_response(history_b, options={"tools": [sync_tool]}),
        )

        contents_a = [c for m in response_a.messages for c in m.contents]
        contents_b = [c for m in response_b.messages for c in m.contents]
        assert all(c is not shared_x for c in contents_a), "A strips its own history echo"
        assert sum(1 for c in contents_b if c is shared_x) == 1, "B lands A's object as fresh work"
        assert any(getattr(c, "text", None) in {"fresh-1", "fresh-2"} for c in contents_a)


# ---------------------------------------------------------------------------
# _merge_loop_messages logic tests (via engine helper)
# ---------------------------------------------------------------------------


class TestMergeLoopMessages:
    """Test the merge logic that inserts recovered loop messages into state."""

    @pytest.mark.parametrize("with_user", [True, False], ids=["last-user-anchor", "no-user"])
    async def test_merge_recovers_loop_messages_once_after_the_last_user(self, with_user: bool) -> None:
        suffix = Message("assistant", ["last response"])
        prefix = [_user("first"), _user("second")] if with_user else []
        messages = [*prefix, suffix]
        history = SessionHistoryManager()
        history.bind({"messages": messages})
        loop = [
            Message("assistant", ["first call"]),
            Message("tool", ["first result"]),
            Message("assistant", ["second call"]),
            Message("tool", ["second result"]),
        ]
        recorder = LoopRecorder()
        await recorder.record_pre_call(prefix)
        await recorder.record_pre_call([*prefix, *loop])

        expected = [*prefix, *loop, suffix] if with_user else [suffix]
        for _ in range(2):
            history.merge_loop_messages(recorder)
            assert messages == expected
            assert all(actual is original for actual, original in zip(messages, expected, strict=True))

    def test_insert_index_places_pass_after_retained_earlier_work(self):
        """The pass boundary beats the user anchor when earlier-pass work follows it.

        Empty-retry repair: the synthetic continuation user message was
        removed before the merge, so the last user message sits BEFORE the
        earlier pass's retained call/result pair. Anchoring to it alone would
        insert this pass's messages ahead of the earlier pass, inverting the
        transcript.
        """
        from chrys.service.session.history import SessionHistoryManager

        user_msg = Message("user", ["run it"])
        pass1_call = Message("assistant", ["invalid call"])
        pass1_result = Message("tool", ["Error: invalid arguments"])
        messages = [user_msg, pass1_call, pass1_result]

        retry_call = Message("assistant", ["valid call"])
        retry_result = Message("tool", ["done"])
        capture = LoopRecorder()
        capture._initial_count = 0
        capture._captured = [retry_call, retry_result]

        history = SessionHistoryManager()
        history.bind({"messages": messages})
        history.merge_loop_messages(capture, insert_index=3)

        assert messages == [user_msg, pass1_call, pass1_result, retry_call, retry_result]

    def test_insert_index_defers_to_later_user_anchor(self):
        """A user message after the boundary (mid-run injection) still anchors the merge."""
        from chrys.service.session.history import SessionHistoryManager

        user_msg = Message("user", ["run it"])
        injection = Message("user", ["also do this"])
        suffix = Message("assistant", ["later output"])
        messages = [user_msg, injection, suffix]

        loop_call = Message("assistant", ["call"])
        loop_result = Message("tool", ["result"])
        capture = LoopRecorder()
        capture._initial_count = 0
        capture._captured = [loop_call, loop_result]

        history = SessionHistoryManager()
        history.bind({"messages": messages})
        history.merge_loop_messages(capture, insert_index=1)

        assert messages == [user_msg, injection, loop_call, loop_result, suffix]


# ---------------------------------------------------------------------------
# Journal commit points and cancellation slots (loop-level)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_post_commit_wire_failure_preserves_exchange_and_blocks_outer_retry(stream: bool) -> None:
    tool_runs = 0
    wire_calls = 0

    @tool(name="commit_once")
    async def commit_once() -> str:
        nonlocal tool_runs
        tool_runs += 1
        return "committed"

    class _FailAfterToolClient:
        def get_response(self, _messages: Any, *, stream: bool = False, **_kwargs: Any) -> Any:
            nonlocal wire_calls
            wire_calls += 1
            if wire_calls <= 2:
                call_id = f"commit-{wire_calls}"
                if not stream:

                    async def _call() -> ChatResponse:
                        return _call_response((call_id, "commit_once", {}))

                    return _call()

                async def _call_updates() -> Any:
                    yield _call_update(call_id, "commit_once", {})

                return ResponseStream(_call_updates(), finalizer=ChatResponse.from_updates)
            if not stream:

                async def _fail() -> ChatResponse:
                    raise ConnectionError("wire failed after commit")

                return _fail()

            async def _fail_updates() -> Any:
                raise ConnectionError("wire failed after commit")
                yield  # pragma: no cover

            return ResponseStream(_fail_updates(), finalizer=ChatResponse.from_updates)

    recorder = LoopRecorder(capture_service_loop_messages=True)
    recorder_snapshot = recorder.snapshot()
    retry = StreamRetryLoop(
        max_retries=2,
        backoff_schedule=(0, 0),
        is_retryable=lambda _exc: True,
        snapshot_history=lambda: HistorySnapshot(messages=[], compressed_count=0),
        restore_history=lambda _snapshot: recorder.restore(recorder_snapshot),
        publish_retry_attempt=lambda *_args: asyncio.sleep(0),
        is_interrupted=lambda: False,
        interruptible_sleep=lambda _seconds: asyncio.sleep(0, result=False),
        clean_error_message=str,
        may_retry=lambda _exc: recorder.committed_count == 0,
    )
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_FailAfterToolClient()))
    attempts = 0

    async def _attempt() -> ChatResponse:
        nonlocal attempts
        attempts += 1
        response = layer.get_response(
            [_user()],
            stream=stream,
            options={"tools": [commit_once], "store": True},
            client_kwargs={"loop_recorder": recorder},
        )
        if not stream:
            return await response
        assert isinstance(response, ResponseStream)
        async for _update in response:
            pass
        return await response.get_final_response()

    with pytest.raises(ConnectionError, match="wire failed after commit"):
        await retry.run(_attempt)

    assert attempts == 1
    assert tool_runs == 2
    assert recorder.committed_count == 2
    projected = recorder.loop_messages
    assert projected is not None
    assert [message.role for message in projected] == ["assistant", "tool", "assistant", "tool"]
    assert [projected[index].contents[0].result for index in (1, 3)] == ["committed", "committed"]

    state = {"messages": [_user()]}
    history = SessionHistoryManager()
    history.bind(state)
    history.merge_loop_messages(recorder, insert_index=1)
    history.trim_to_last_complete_tool_results()
    history.insert_interrupted_marker(reason="wire failed after commit", source="error")
    assert [
        content.call_id
        for message in history.messages
        for content in message.contents
        if content.type == "function_result"
    ] == ["commit-1", "commit-2"]
    assert history.messages[-1].additional_properties[HistoryMarkerKind.KEY] == HistoryMarkerKind.INTERRUPTED
    assert history.messages[-1].additional_properties["_interrupted_by"] == "error"


@pytest.mark.asyncio
async def test_post_execution_middleware_cancel_keeps_raw_result_and_marks_processing_interrupted() -> None:
    tool_runs = 0
    tool_returned = asyncio.Event()
    hold_post_processing = asyncio.Event()

    @tool(name="returned_tool")
    async def returned_tool() -> str:
        nonlocal tool_runs
        tool_runs += 1
        return "raw-result"

    class _HoldAfterReturn(FunctionMiddleware):
        async def process(
            self,
            _context: FunctionInvocationContext,
            call_next: Callable[[], Awaitable[None]],
        ) -> None:
            await call_next()
            tool_returned.set()
            await hold_post_processing.wait()

    measured_timing = {
        "started_at": "2026-08-19T01:02:03.000000+00:00",
        "finished_at": "2026-08-19T01:02:03.025000+00:00",
        "duration_ms": 25,
    }

    class _StampTimingOnExit(FunctionMiddleware):
        async def process(
            self,
            context: FunctionInvocationContext,
            call_next: Callable[[], Awaitable[None]],
        ) -> None:
            try:
                await call_next()
            finally:
                context.metadata[TRAJECTORY_TIMING_KEY] = dict(measured_timing)

    recorder = LoopRecorder()
    layer, _wire = _stack([_call_response(("c1", "returned_tool", {}))])
    task = asyncio.create_task(
        layer.get_response(
            [_user()],
            options={"tools": [returned_tool]},
            middleware=[_StampTimingOnExit(), _HoldAfterReturn()],
            client_kwargs={"loop_recorder": recorder},
        )
    )
    try:
        await wait_for(lambda: tool_returned.is_set() or task.done())
        if task.done():
            await task
            pytest.fail("tool call finished before the cancellation point")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    projected = recorder.loop_messages
    assert projected is not None
    result = projected[-1].contents[0]
    call = projected[0].contents[0]
    metadata = result.additional_properties[TOOL_RESULT_METADATA_KEY]
    # No result hooks were declared (HOOK_RESULT_PENDING_KEY unset), so nothing
    # would have changed the output: the raw result is final and is kept.
    assert result.result == "raw-result"
    assert metadata[TOOL_INTERRUPTED_METADATA_KEY] is True
    assert metadata[TOOL_POST_PROCESSING_INTERRUPTED_METADATA_KEY] is True
    assert call.additional_properties[TRAJECTORY_TIMING_KEY] == measured_timing
    assert result.additional_properties[TRAJECTORY_TIMING_KEY] == measured_timing
    assert tool_runs == 1


@pytest.mark.asyncio
async def test_cancel_while_result_hooks_run_restores_a_tombstone_not_the_raw_output() -> None:
    """A pending result hook keeps the raw output out of resumed history."""
    tool_returned = asyncio.Event()
    hold_hooks = asyncio.Event()

    @tool(name="secret_tool")
    async def secret_tool() -> str:
        return "PRIVATE_CREDENTIAL_123"

    class _PendingHooks(FunctionMiddleware):
        async def process(
            self,
            context: FunctionInvocationContext,
            call_next: Callable[[], Awaitable[None]],
        ) -> None:
            context.metadata[HOOK_RESULT_PENDING_KEY] = True
            await call_next()
            tool_returned.set()
            await hold_hooks.wait()

    recorder = LoopRecorder()
    layer, _wire = _stack([_call_response(("c1", "secret_tool", {}))])
    task = asyncio.create_task(
        layer.get_response(
            [_user()],
            options={"tools": [secret_tool]},
            middleware=[_PendingHooks()],
            client_kwargs={"loop_recorder": recorder},
        )
    )
    try:
        await wait_for(lambda: tool_returned.is_set() or task.done())
        if task.done():
            await task
            pytest.fail("tool call finished before the cancellation point")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    projected = recorder.loop_messages
    assert projected is not None
    result = projected[-1].contents[0]
    metadata = result.additional_properties[TOOL_RESULT_METADATA_KEY]
    assert "PRIVATE_CREDENTIAL_123" not in str(result.result)
    assert result.result.startswith("Error: Tool execution was interrupted.")
    assert metadata[TOOL_INTERRUPTED_METADATA_KEY] is True
    assert metadata[TOOL_POST_PROCESSING_INTERRUPTED_METADATA_KEY] is True


@pytest.mark.asyncio
async def test_sync_completion_cancel_with_result_hooks_declared_restores_a_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync worker finishing as the cancel lands must not bypass declared redaction hooks."""

    @tool(name="threaded_tool")
    def threaded_tool() -> str:
        return "unused"

    async def _cancelled_after_completion(self: FunctionTool, call_kwargs: Any) -> Any:
        raise SyncToolCancelledAfterCompletion("PRIVATE_CREDENTIAL_123")

    class _DeclareResultHooks(FunctionMiddleware):
        async def process(
            self,
            context: FunctionInvocationContext,
            call_next: Callable[[], Awaitable[None]],
        ) -> None:
            context.metadata[HOOK_RESULT_PENDING_KEY] = True
            await call_next()

    monkeypatch.setattr(FunctionTool, "_invoke_function", _cancelled_after_completion)
    recorder = LoopRecorder()
    layer, _wire = _stack([_call_response(("fast", "threaded_tool", {}))])
    with pytest.raises(asyncio.CancelledError):
        await layer.get_response(
            [_user()],
            options={"tools": [threaded_tool]},
            middleware=[_DeclareResultHooks()],
            client_kwargs={"loop_recorder": recorder},
        )

    projected = recorder.loop_messages
    assert projected is not None
    result = projected[-1].contents[0]
    assert "PRIVATE_CREDENTIAL_123" not in str(result.result)
    assert result.result.startswith("Error: Tool execution was interrupted.")
    assert recorder.committed_count == 1


@pytest.mark.asyncio
async def test_post_execution_middleware_cancel_uses_processed_result_when_context_result_set() -> None:
    """When middleware modifies context.result before cancellation, the slot keeps the processed value.

    Scenario: tool returns "raw-result"; a middleware replaces context.result with
    "processed-result" (simulating what hooks/redaction middleware does), then
    awaits a gate. Cancellation arrives while the gate holds. The slot must end up
    with "processed-result", not "raw-result", because middleware already finished
    its work and the processed value is what the model should see.
    """
    tool_returned = asyncio.Event()
    hold_after_modify = asyncio.Event()

    @tool(name="modifying_tool")
    async def modifying_tool() -> str:
        return "raw-result"

    class _ModifyThenHold(FunctionMiddleware):
        async def process(
            self,
            context: FunctionInvocationContext,
            call_next: Callable[[], Awaitable[None]],
        ) -> None:
            await call_next()
            # Simulate what hook/redaction middleware does: replace the result
            # and set the processing-complete marker.
            context.result = "processed-result"
            if isinstance(context.metadata, dict):
                context.metadata[HOOK_RESULT_APPLIED_KEY] = True
            tool_returned.set()
            await hold_after_modify.wait()

    recorder = LoopRecorder()
    layer, _wire = _stack([_call_response(("c1", "modifying_tool", {}))])
    task = asyncio.create_task(
        layer.get_response(
            [_user()],
            options={"tools": [modifying_tool]},
            middleware=[_ModifyThenHold()],
            client_kwargs={"loop_recorder": recorder},
        )
    )
    try:
        await wait_for(lambda: tool_returned.is_set() or task.done())
        if task.done():
            await task
            pytest.fail("tool call finished before the cancellation point")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    projected = recorder.loop_messages
    assert projected is not None
    result = projected[-1].contents[0]
    metadata = result.additional_properties[TOOL_RESULT_METADATA_KEY]
    # The middleware-processed value must survive; raw-result must not reach the model.
    assert result.result == "processed-result"
    assert metadata[TOOL_INTERRUPTED_METADATA_KEY] is True
    assert metadata[TOOL_POST_PROCESSING_INTERRUPTED_METADATA_KEY] is True


@pytest.mark.asyncio
async def test_early_interrupted_slot_upgrades_timing_after_middleware_unwinds() -> None:
    """A cascade-style early fill accepts measured timing without replacing its result."""
    measured_timing = {
        "started_at": "2026-08-19T01:02:03.000000+00:00",
        "finished_at": "2026-08-19T01:02:03.025000+00:00",
        "duration_ms": 25,
    }

    class _StampTimingOnExit(FunctionMiddleware):
        async def process(
            self,
            context: FunctionInvocationContext,
            call_next: Callable[[], Awaitable[None]],
        ) -> None:
            try:
                await call_next()
            finally:
                context.metadata[TRAJECTORY_TIMING_KEY] = dict(measured_timing)

    class _CommitThenCancel(FunctionMiddleware):
        async def process(
            self,
            context: FunctionInvocationContext,
            _call_next: Callable[[], Awaitable[None]],
        ) -> None:
            callback = context.metadata[SUB_AGENT_RESULT_COMMIT_CALLBACK_KEY]
            assert callable(callback)
            callback({"sub_agent_invocation_id": "inv-1"})
            raise asyncio.CancelledError

    recorder = LoopRecorder()
    layer, _wire = _stack([_call_response(("c1", "echo", {"text": "unused"}))])

    with pytest.raises(asyncio.CancelledError):
        await layer.get_response(
            [_user()],
            options={"tools": [_make_tool()]},
            middleware=[_StampTimingOnExit(), _CommitThenCancel()],
            client_kwargs={"loop_recorder": recorder},
        )

    projected = recorder.loop_messages
    assert projected is not None
    call = projected[0].contents[0]
    result = projected[1].contents[0]
    assert result.additional_properties[TOOL_RESULT_METADATA_KEY]["sub_agent_invocation_id"] == "inv-1"
    assert call.additional_properties[TRAJECTORY_TIMING_KEY] == measured_timing
    assert result.additional_properties[TRAJECTORY_TIMING_KEY] == measured_timing


@pytest.mark.asyncio
async def test_batch_failure_waits_for_slower_sibling_commit_before_propagating() -> None:
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    slow_finished = False

    @tool(name="slow_sibling")
    async def slow_sibling() -> str:
        nonlocal slow_finished
        slow_started.set()
        await release_slow.wait()
        slow_finished = True
        return "slow-done"

    missing_id = Content.from_function_call(None, "slow_sibling", arguments={})
    valid = Content.from_function_call("slow", "slow_sibling", arguments={})
    recorder = LoopRecorder()
    layer, _wire = _stack([ChatResponse(messages=[Message("assistant", [missing_id, valid])])])
    task = asyncio.create_task(
        layer.get_response(
            [_user()],
            options={"tools": [slow_sibling]},
            client_kwargs={"loop_recorder": recorder},
        )
    )
    await slow_started.wait()
    await asyncio.sleep(0)
    assert not task.done()

    release_slow.set()
    with pytest.raises(KeyError, match="missing call_id"):
        await task

    assert slow_finished is True
    projected = recorder.loop_messages
    assert projected is not None
    assert [content.call_id for content in projected[0].contents if content.type == "function_call"] == ["slow"]
    assert projected[1].contents[0].result == "slow-done"


@pytest.mark.asyncio
async def test_sync_worker_cancel_commits_interrupted_slot_and_keeps_falsy_sibling_alignment() -> None:
    slow_started = ThreadEvent()
    release_slow = ThreadEvent()
    fast_finished = ThreadEvent()

    @tool(name="threaded_batch")
    def threaded_batch(kind: str) -> str:
        if kind == "slow":
            slow_started.set()
            # Generous timeout: an early expiry would let the worker finish
            # while cancellation is in flight, where draining it as a commit
            # is legitimate — and the slot assertions below would flip.
            release_slow.wait(timeout=30)
            return "slow-returned-after-cancel"
        fast_finished.set()
        return "fast-result"

    slow_call = Content.from_function_call("", "threaded_batch", arguments={"kind": "slow"})
    fast_call = Content.from_function_call("", "threaded_batch", arguments={"kind": "fast"})
    recorder = LoopRecorder()
    layer, _wire = _stack([ChatResponse(messages=[Message("assistant", [slow_call, fast_call])])])
    task = asyncio.create_task(
        layer.get_response(
            [_user()],
            options={"tools": [threaded_batch]},
            client_kwargs={"loop_recorder": recorder},
        )
    )
    assert await asyncio.to_thread(slow_started.wait, 2)
    assert await asyncio.to_thread(fast_finished.wait, 2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Release only after the run settled: a worker completing while
    # cancellation is still in flight is legitimately drained as a commit,
    # which would make the slow slot's outcome interleaving-dependent.
    release_slow.set()

    projected = recorder.loop_messages
    assert projected is not None
    assert projected[0].contents == [slow_call, fast_call]
    assert len(projected[1].contents) == 2
    slow_metadata = projected[1].contents[0].additional_properties[TOOL_RESULT_METADATA_KEY]
    assert slow_metadata[TOOL_INTERRUPTED_METADATA_KEY] is True
    assert projected[1].contents[1].result == "fast-result"
    assert recorder.committed_count == 1


@pytest.mark.asyncio
async def test_sync_worker_result_drained_on_cancel_commits_raw_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drained completed value reaches the journal as a raw commit before cancellation proceeds."""

    @tool(name="threaded_tool")
    def threaded_tool() -> str:
        return "unused"

    async def _cancelled_after_completion(self: FunctionTool, call_kwargs: Any) -> Any:
        raise SyncToolCancelledAfterCompletion("fast-result")

    monkeypatch.setattr(FunctionTool, "_invoke_function", _cancelled_after_completion)
    recorder = LoopRecorder()
    layer, _wire = _stack([_call_response(("fast", "threaded_tool", {}))])
    with pytest.raises(asyncio.CancelledError):
        await layer.get_response(
            [_user()],
            options={"tools": [threaded_tool]},
            client_kwargs={"loop_recorder": recorder},
        )

    projected = recorder.loop_messages
    assert projected is not None
    result = projected[-1].contents[0]
    metadata = result.additional_properties[TOOL_RESULT_METADATA_KEY]
    assert result.result == "fast-result"
    assert metadata[TOOL_INTERRUPTED_METADATA_KEY] is True
    assert metadata[TOOL_POST_PROCESSING_INTERRUPTED_METADATA_KEY] is True
    assert recorder.committed_count == 1
