# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pipeline integration tests for tool approval end to end.

Scenario families S4 (auto-approve), S5 (user approve/reject and interrupt
while waiting), S8 (approval preserves intermediate tool calls), S9 (several
approval-gated tools in one turn) and S10 (compression plus approval).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import pytest

from chrys.app.tui.screens.main.dialog_controllers import ApprovalQueueController
from chrys.foundation.events.types import (
    ApprovalCancelled,
    ApprovalRequest,
    ApprovalReviewed,
    SetApprovalMode,
    UserMessage,
)
from chrys.foundation.models.approval_reuse import ReuseChoice
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.tool_kinds import KIND_SKILL
from chrys.service.approval.judge import ApprovalJudge, JudgeVerdict
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.agents.schema import SkillConfig, SkillsConfig
from chrys.service.skills.constants import RUN_SKILL_SCRIPT_TOOL_NAME
from tests.support.pipeline_helpers import (
    extract_approval_requests,
    extract_final_messages,
    extract_session_approvals,
    extract_session_call_ids,
    extract_session_tool_names,
    extract_tool_results,
    extract_tool_starts,
    wait_for_event,
    wait_for_idle,
)
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from chrys.app.tui.screens.main.dialog_controllers import ApprovalBypassDecision, ApprovalDialogHandle

# ---------------------------------------------------------------------------
# S4: Approval auto-approve with rollback
# ---------------------------------------------------------------------------


class TestAutoApproval:
    """With default="auto", all tools execute without approval — no ApprovalRequest events."""

    @pytest.fixture
    async def ctx(self, make_pipeline_ctx):
        responses = [
            # Both guarded and normal tools execute inline (no approval needed)
            MockResponse(
                tool_calls=[
                    ("echo", "a1", {"message": "safe"}),
                    ("guarded_echo", "a2", {"message": "also safe with auto"}),
                ]
            ),
            MockResponse(text="Tools executed without approval"),
        ]
        return await make_pipeline_ctx(responses, approval_default="auto")

    async def test_auto_approval_events(self, ctx):
        """Events: no ApprovalRequest published when policy is auto."""
        await ctx.send_message("Run tools")

        approval_reqs = extract_approval_requests(ctx.events)
        assert len(approval_reqs) == 0

        # Both tools should execute
        results = extract_tool_results(ctx.events)
        assert len(results) == 2

        finals = extract_final_messages(ctx.events)
        assert len(finals) == 1

    async def test_session_no_approval_tags(self, ctx):
        """Session: no _approval tags when all tools are auto (no approval needed)."""
        await ctx.send_message("Run tools")

        raw = await ctx.get_session_messages()
        approvals = extract_session_approvals(raw)

        # No approval tags — tools just execute normally
        tagged = [a for a in approvals if a is not None]
        assert len(tagged) == 0


class TestRuntimeSkillApproval:
    """The real build/provider/kernel chain preserves live skill provenance."""

    async def test_built_agent_gates_runtime_skill_script_under_auto_default(self, make_pipeline_ctx) -> None:
        responses = [
            MockResponse(
                tool_calls=[
                    (
                        RUN_SKILL_SCRIPT_TOOL_NAME,
                        "skill-call",
                        {"skill_name": "review", "script_name": "scripts/review.py"},
                    )
                ]
            ),
            MockResponse(text="Rejected script handled"),
        ]
        skills = SkillsConfig(
            inline=[SkillConfig(name="review", description="Review files", instructions="Review carefully.")],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        )
        ctx = await make_pipeline_ctx(responses, approval_default="auto", skills=skills)
        await ctx.bus.publish(UserMessage(text="Run the review skill"))

        approval_events = await wait_for_event(ctx.events, ApprovalRequest, timeout=20.0)
        request = approval_events[0]
        assert request.tool_name == RUN_SKILL_SCRIPT_TOOL_NAME
        assert request.tool_kind == KIND_SKILL

        await ctx.reject(request.request_id, reason="untrusted skill")
        await wait_for_idle(ctx)

        results = extract_tool_results(ctx.events)
        assert len(results) == 1
        assert results[0]["tool_name"] == RUN_SKILL_SCRIPT_TOOL_NAME
        assert "rejected by user" in results[0]["result"]


# ---------------------------------------------------------------------------
# S5: User approval — approve and reject
# ---------------------------------------------------------------------------


class TestUserApproval:
    """guarded_echo with override require — user must approve/reject via ApprovalMiddleware."""

    async def test_user_approve(self, make_pipeline_ctx):
        """User approves the tool → tool executes."""
        responses = [
            # LLM calls guarded_echo → ApprovalMiddleware pauses for approval
            MockResponse(tool_calls=[("guarded_echo", "u1", {"message": "please approve"})]),
            # After approval + tool execution, LLM responds with text
            MockResponse(text="Approved and done"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_overrides={"guarded_echo": "require"})

        await ctx.bus.publish(UserMessage(text="Run guarded tool"))

        approval_events = await wait_for_event(ctx.events, ApprovalRequest, timeout=20.0)
        assert len(approval_events) >= 1
        request_id = approval_events[0].request_id

        # Approve
        await ctx.approve(request_id)
        await wait_for_idle(ctx)

        finals = extract_final_messages(ctx.events)
        assert len(finals) >= 1

        # Session should show user_approved
        raw = await ctx.get_session_messages()
        approvals = extract_session_approvals(raw)
        user_approved = [a for a in approvals if a is not None and a.get("status") == "user_approved"]
        assert len(user_approved) >= 1

    async def test_user_reject(self, make_pipeline_ctx):
        """User rejects the tool → LLM gets rejection error, responds with text."""
        responses = [
            MockResponse(tool_calls=[("guarded_echo", "r1", {"message": "reject me"})]),
            # After rejection (error result), LLM responds with text
            MockResponse(text="OK, I won't run that tool."),
        ]
        ctx = await make_pipeline_ctx(responses, approval_overrides={"guarded_echo": "require"})

        await ctx.bus.publish(UserMessage(text="Run guarded tool"))

        approval_events = await wait_for_event(ctx.events, ApprovalRequest, timeout=20.0)
        request_id = approval_events[0].request_id

        # Reject with a reason so the model can steer its next response.
        rejection_reason = "Use a safer command"
        await ctx.reject(request_id, reason=rejection_reason)
        await wait_for_idle(ctx)

        finals = extract_final_messages(ctx.events)
        assert len(finals) >= 1

        # Rejected tool should still produce ToolCallStart/Result events
        # (middleware order: tool_events wraps approval)
        starts = extract_tool_starts(ctx.events)
        assert any(s["tool_name"] == "guarded_echo" for s in starts)
        results = extract_tool_results(ctx.events)
        rejected_results = [r for r in results if "rejected" in r["result"].lower()]
        assert len(rejected_results) >= 1
        assert any(rejection_reason in r["result"] for r in rejected_results)

        # Session should show user_rejected and preserve the reason.
        raw = await ctx.get_session_messages()
        approvals = extract_session_approvals(raw)
        user_rejected = [a for a in approvals if a is not None and a.get("status") == "user_rejected"]
        assert len(user_rejected) >= 1
        assert any(a.get("reason") == rejection_reason for a in user_rejected)

    async def test_reused_provider_call_id_still_requires_each_approval(self, make_pipeline_ctx) -> None:
        """Provider call IDs are not approval identities; every invocation gates independently."""
        responses = [
            MockResponse(tool_calls=[("guarded_echo", "reused-id", {"message": "first"})]),
            MockResponse(tool_calls=[("guarded_echo", "reused-id", {"message": "second"})]),
            MockResponse(text="Both calls approved"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_overrides={"guarded_echo": "require"})
        await ctx.bus.publish(UserMessage(text="Run both guarded calls"))

        first_events = await wait_for_event(ctx.events, ApprovalRequest, timeout=20.0)
        await ctx.approve(first_events[0].request_id)

        both_events = await wait_for_event(ctx.events, ApprovalRequest, timeout=20.0, min_count=2)
        assert both_events[0].request_id != both_events[1].request_id
        await ctx.approve(both_events[1].request_id)
        await wait_for_idle(ctx)

        guarded_results = [
            result for result in extract_tool_results(ctx.events) if result["tool_name"] == "guarded_echo"
        ]
        assert len(guarded_results) == 2


# ---------------------------------------------------------------------------
# S5b: Interrupt during approval wait
# ---------------------------------------------------------------------------


class TestInterruptDuringApproval:
    """Interrupt while ApprovalMiddleware is waiting for user response."""

    async def test_interrupt_during_approval_preserves_prior_work(self, make_pipeline_ctx):
        """Interrupt while waiting for approval: prior tools preserved, no crash."""
        responses = [
            # LLM calls echo (auto) then guarded_echo (requires approval)
            MockResponse(tool_calls=[("echo", "e1", {"message": "before"})]),
            MockResponse(tool_calls=[("guarded_echo", "g1", {"message": "needs approval"})]),
            # Resume responses
            MockResponse(tool_calls=[("echo", "e2", {"message": "resumed"})]),
            MockResponse(text="Resumed OK"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_overrides={"guarded_echo": "require"})

        await ctx.bus.publish(UserMessage(text="Run tools"))

        # Wait for the approval request to appear
        approval_events = await wait_for_event(ctx.events, ApprovalRequest, timeout=20.0)
        assert len(approval_events) >= 1

        # Interrupt instead of approving/rejecting
        await ctx.send_interrupt()
        await wait_for_idle(ctx)

        # Verify prior echo tool is preserved in session
        raw = await ctx.get_session_messages()
        tool_names = extract_session_tool_names(raw)
        assert "echo" in tool_names

        # Resume after interrupt
        ctx.events.clear()
        await ctx.send_retry()

        finals = extract_final_messages(ctx.events)
        assert len(finals) >= 1


class _DeferringApprovalPort:
    """The TUI side of the approval queue with ``ui.approval.defer_while_judging`` on.

    Records the header's review count and anything that would reach the user.
    """

    def __init__(self) -> None:
        self.review_counts: list[int] = []
        self.surfaced: list[str] = []

    async def build_approval_body(self, event: ApprovalRequest) -> object | None:
        return None

    def approval_body_bypass(self, body: object | None) -> ApprovalBypassDecision | None:
        return None

    def show_approval_dialog(
        self,
        event: ApprovalRequest,
        approval_body: object | None,
        on_result: Callable[[tuple[bool, str, dict[str, Any] | None] | None], None],
        *,
        verdict: ApprovalReviewed | None,
    ) -> ApprovalDialogHandle:
        self.surfaced.append(f"dialog {event.request_id}")
        return SimpleNamespace(user_decision_submitted=False, remember_choice="", is_dismissed=False)

    def deliver_approval_verdict(self, dialog: ApprovalDialogHandle, event: ApprovalReviewed) -> None:
        self.surfaced.append(f"verdict {event.request_id}")

    def dismiss_approval_dialog(self, dialog: ApprovalDialogHandle) -> None:
        self.surfaced.append("dismiss")

    def approval_dialog_tool_name(self, dialog: ApprovalDialogHandle) -> str:
        return ""

    def approval_defer_while_judging(self) -> bool:
        return True

    def set_auto_review_count(self, count: int) -> None:
        self.review_counts.append(count)

    def debug(self, key: str, message: str = "") -> None:
        return None

    def notify_approval_required(self) -> None:
        self.surfaced.append("notify")

    def update_tool_args(self, call_id: str, args: dict[str, Any]) -> None:
        self.surfaced.append(f"args {call_id}")

    def handle_approval_response(
        self,
        request_id: str,
        approved: bool,
        reason: str,
        modified_args: dict[str, Any] | None = None,
        remember_choice: ReuseChoice = "",
    ) -> None:
        self.surfaced.append(f"response {request_id}")

    def run_worker(self, awaitable: Awaitable[Any], *, group: str) -> None:
        self.surfaced.append(f"worker {group}")
        if isinstance(awaitable, Coroutine):
            awaitable.close()

    async def publish_auto_fulfill_blocked(self, event: ApprovalReviewed) -> None:
        self.surfaced.append(f"blocked {event.request_id}")


class TestInterruptWhileTheJudgeReviews:
    """An interrupt during review retracts the request the TUI is hiding."""

    async def test_the_tui_keeps_nothing_hidden_or_counted_after_the_interrupt(
        self, make_pipeline_ctx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        cancelled: list[str] = []

        async def held_evaluate(
            _judge: ApprovalJudge,
            user_message: str,
            tool_name: str,
            tool_kind: str,
            args: dict[str, Any],
            workspace_roots: list[str],
            request_id: str = "",
            log_dir: Path | None = None,
            user_messages: list[str] | None = None,
            relay_enhanced: bool = False,
            reviewed_model: str = "",
        ) -> JudgeVerdict:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(request_id)
                raise
            raise AssertionError("the judge was released instead of cancelled")

        monkeypatch.setattr(
            ApprovalJudge, "evaluate", create_autospec(ApprovalJudge.evaluate, side_effect=held_evaluate)
        )
        responses = [
            MockResponse(tool_calls=[("guarded_echo", "g1", {"message": "judged"})]),
            MockResponse(text="never reached"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_overrides={"guarded_echo": "require"})
        await ctx.bus.publish(SetApprovalMode(mode="auto", persist=False), raise_handler_errors=True)
        port = _DeferringApprovalPort()
        controller = ApprovalQueueController(port)
        await ctx.bus.subscribe(ApprovalRequest, controller.on_request)
        await ctx.bus.subscribe(ApprovalReviewed, controller.on_reviewed)
        await ctx.bus.subscribe(ApprovalCancelled, controller.on_cancelled)

        def turn_ended() -> bool:
            task = ctx.engine.turns.turn_state.lease.run_task
            return task is not None and task.done()

        try:
            await ctx.bus.publish(UserMessage(text="Run the guarded call"))
            await wait_for(
                lambda: entered.is_set() or turn_ended(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="the judge reviewing the guarded call",
            )
            assert entered.is_set(), "the turn ended before its call reached the judge"
            requested = [request["request_id"] for request in extract_approval_requests(ctx.events)]
            assert list(controller.deferred) == requested
            assert port.review_counts == [1]

            await ctx.send_interrupt()
            await wait_for_idle(ctx)
        finally:
            release.set()

        assert cancelled == requested
        assert controller.deferred == {}
        assert port.review_counts == [1, 0]
        assert port.surfaced == []


# ---------------------------------------------------------------------------
# S8: Approval preserves intermediate tool calls
# ---------------------------------------------------------------------------


class TestApprovalPreservesIntermediateTools:
    """When a non-guarded tool (echo) runs before a guarded tool (guarded_echo),
    the approval re-submission loop must preserve the intermediate echo call
    in the session state with correct ordering:
    user → echo_call → echo_result → guarded_call → guarded_result → final_text.

    With inline approval via ``ApprovalMiddleware``, intermediate tools are
    naturally preserved — no snapshot/restore or loop-message merging needed.
    These tests verify the correct ordering and uniqueness of tool calls in
    sessions with both approved and non-approved tools.
    """

    async def test_intermediate_tool_preserved_auto(self, make_pipeline_ctx):
        """Auto (no approval): echo + guarded_echo both execute and appear in session."""
        responses = [
            MockResponse(tool_calls=[("echo", "e1", {"message": "intermediate"})]),
            MockResponse(tool_calls=[("guarded_echo", "g1", {"message": "guarded"})]),
            MockResponse(text="All done"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_default="auto")
        await ctx.send_message("Run both tools")

        raw = await ctx.get_session_messages()
        tool_names = extract_session_tool_names(raw)
        assert "echo" in tool_names, f"echo lost! Tools: {tool_names}"
        assert "guarded_echo" in tool_names

    async def test_intermediate_tool_preserved_user_approval(self, make_pipeline_ctx):
        """User-approve: echo runs before guarded_echo — both in session."""
        responses = [
            # LLM call 1: echo (no approval needed, executes immediately)
            MockResponse(tool_calls=[("echo", "e1", {"message": "intermediate"})]),
            # LLM call 2: guarded_echo (middleware pauses for approval)
            MockResponse(tool_calls=[("guarded_echo", "g1", {"message": "guarded"})]),
            # LLM call 3: final text
            MockResponse(text="User approved and done"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_overrides={"guarded_echo": "require"})
        await ctx.bus.publish(UserMessage(text="Run both tools"))

        approval_events = await wait_for_event(ctx.events, ApprovalRequest, timeout=20.0)
        await ctx.approve(approval_events[0].request_id)
        await wait_for_idle(ctx)

        raw = await ctx.get_session_messages()
        tool_names = extract_session_tool_names(raw)
        assert "echo" in tool_names, f"echo lost! Tools: {tool_names}"
        assert "guarded_echo" in tool_names

        # Verify ordering: user → echo → guarded_echo
        echo_idx = guarded_idx = user_idx = None
        for i, m in enumerate(raw):
            if m.get("role") == "user" and user_idx is None:
                user_idx = i
            for c in m.get("contents", []):
                if isinstance(c, dict) and c.get("type") == "function_call":
                    if c.get("name") == "echo" and echo_idx is None:
                        echo_idx = i
                    elif c.get("name") == "guarded_echo" and guarded_idx is None:
                        guarded_idx = i
        assert user_idx is not None and echo_idx is not None and guarded_idx is not None
        assert user_idx < echo_idx < guarded_idx

    async def test_multiple_intermediate_tools_preserved_exactly_once(self, make_pipeline_ctx):
        """Multiple intermediate tools (echo, concat) before guarded — each kept exactly once."""
        responses = [
            MockResponse(tool_calls=[("echo", "e1", {"message": "first"})]),
            MockResponse(tool_calls=[("concat", "c1", {"a": "x", "b": "y"})]),
            MockResponse(tool_calls=[("guarded_echo", "g1", {"message": "guarded"})]),
            MockResponse(text="All three executed"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_default="auto")
        await ctx.send_message("Run all three tools")

        raw = await ctx.get_session_messages()
        tool_names = extract_session_tool_names(raw)
        assert tool_names.count("echo") == 1, f"echo lost or duplicated! Tools: {tool_names}"
        assert tool_names.count("concat") == 1, f"concat lost or duplicated! Tools: {tool_names}"
        assert tool_names.count("guarded_echo") == 1, f"guarded_echo lost or duplicated! Tools: {tool_names}"

    async def test_no_approval_normal_completion(self, make_pipeline_ctx):
        """Without approval tools, normal completion produces clean session."""
        responses = [
            MockResponse(tool_calls=[("echo", "e1", {"message": "first"})]),
            MockResponse(tool_calls=[("concat", "c1", {"a": "a", "b": "b"})]),
            MockResponse(text="Normal completion"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_default="auto")
        await ctx.send_message("Run normal tools")

        raw = await ctx.get_session_messages()
        tool_names = extract_session_tool_names(raw)
        assert tool_names == ["echo", "concat"], f"Unexpected tools: {tool_names}"

        non_marker = [
            m["role"]
            for m in raw
            if m.get("additional_properties", {}).get(HistoryMarkerKind.KEY) != HistoryMarkerKind.TURN
        ]
        assert non_marker == ["user", "assistant", "tool", "assistant", "tool", "assistant"]


# ---------------------------------------------------------------------------
# S9: Multi-approval iterations (no duplicate messages)
# ---------------------------------------------------------------------------


class TestMultipleApprovalTools:
    """Multiple approval-needing tools in a single turn execute inline via
    ``ApprovalMiddleware`` — each is individually approved within the
    Chrys tool loop. No snapshot/restore, no loop-message capture
    between iterations.

    Real-world pattern: read_file → write_file(approval) → read_file →
    write_file(approval) → text.
    """

    async def test_two_guarded_tools_auto(self, make_pipeline_ctx):
        """Auto: echo → guarded → echo → guarded → text. All execute inline."""
        responses = [
            MockResponse(tool_calls=[("echo", "e1", {"message": "read1"})]),
            MockResponse(tool_calls=[("guarded_echo", "g1", {"message": "write1"})]),
            MockResponse(tool_calls=[("echo", "e2", {"message": "read2"})]),
            MockResponse(tool_calls=[("guarded_echo", "g2", {"message": "write2"})]),
            MockResponse(text="Both writes done"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_default="auto")
        await ctx.send_message("Read and write two files")

        raw = await ctx.get_session_messages()
        tool_names = extract_session_tool_names(raw)
        call_ids = extract_session_call_ids(raw)

        assert tool_names.count("echo") == 2
        assert tool_names.count("guarded_echo") == 2
        assert len(call_ids) == len(set(call_ids)), f"Duplicate call_ids: {call_ids}"
        assert tool_names == ["echo", "guarded_echo", "echo", "guarded_echo"]

    async def test_two_guarded_tools_user_approve(self, make_pipeline_ctx):
        """User-approve: echo → guarded → echo → guarded → text.
        Two user approvals — each pauses the tool loop inline."""
        responses = [
            MockResponse(tool_calls=[("echo", "e1", {"message": "read1"})]),
            MockResponse(tool_calls=[("guarded_echo", "g1", {"message": "write1"})]),
            MockResponse(tool_calls=[("echo", "e2", {"message": "read2"})]),
            MockResponse(tool_calls=[("guarded_echo", "g2", {"message": "write2"})]),
            MockResponse(text="Both writes done"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_overrides={"guarded_echo": "require"})
        await ctx.bus.publish(UserMessage(text="Read and write two files"))

        # First approval
        events1 = await wait_for_event(ctx.events, ApprovalRequest, timeout=20.0)
        await ctx.approve(events1[0].request_id)

        # Second approval
        events2 = await wait_for_event(ctx.events, ApprovalRequest, timeout=20.0, min_count=2)
        await ctx.approve(events2[1].request_id)
        await wait_for_idle(ctx)

        raw = await ctx.get_session_messages()
        tool_names = extract_session_tool_names(raw)
        call_ids = extract_session_call_ids(raw)

        assert tool_names.count("echo") == 2
        assert tool_names.count("guarded_echo") == 2
        assert len(call_ids) == len(set(call_ids)), f"Duplicate call_ids: {call_ids}"

    async def test_three_guarded_tools(self, make_pipeline_ctx):
        """Three consecutive guarded tools — stress test."""
        responses = [
            MockResponse(tool_calls=[("echo", "e1", {"message": "r1"})]),
            MockResponse(tool_calls=[("guarded_echo", "g1", {"message": "w1"})]),
            MockResponse(tool_calls=[("concat", "c1", {"a": "x", "b": "y"})]),
            MockResponse(tool_calls=[("guarded_echo", "g2", {"message": "w2"})]),
            MockResponse(tool_calls=[("echo", "e2", {"message": "r2"})]),
            MockResponse(tool_calls=[("guarded_echo", "g3", {"message": "w3"})]),
            MockResponse(text="All three writes done"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_default="auto")
        await ctx.send_message("Three rounds of read-write")

        raw = await ctx.get_session_messages()
        tool_names = extract_session_tool_names(raw)
        call_ids = extract_session_call_ids(raw)

        assert tool_names.count("echo") == 2
        assert tool_names.count("concat") == 1
        assert tool_names.count("guarded_echo") == 3
        assert len(call_ids) == len(set(call_ids)), f"Duplicate call_ids: {call_ids}"

    async def test_multi_approval_second_turn(self, make_pipeline_ctx):
        """Multiple guarded tools in the second turn — verifies turn boundary handling."""
        responses = [
            # Turn 1: simple, no approval
            MockResponse(tool_calls=[("echo", "t1e1", {"message": "turn1"})]),
            MockResponse(text="Turn 1 done"),
            # Turn 2: echo → guarded → echo → guarded → text
            MockResponse(tool_calls=[("echo", "t2e1", {"message": "read1"})]),
            MockResponse(tool_calls=[("guarded_echo", "t2g1", {"message": "write1"})]),
            MockResponse(tool_calls=[("echo", "t2e2", {"message": "read2"})]),
            MockResponse(tool_calls=[("guarded_echo", "t2g2", {"message": "write2"})]),
            MockResponse(text="Turn 2 done"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_default="auto")
        await ctx.send_message("Turn 1")
        await ctx.send_message("Turn 2 with guarded tools")

        raw = await ctx.get_session_messages()
        call_ids = extract_session_call_ids(raw)

        assert len(call_ids) == len(set(call_ids)), f"Duplicate call_ids: {call_ids}"
        tool_names = extract_session_tool_names(raw)
        assert tool_names.count("echo") == 3  # 1 from turn1, 2 from turn2
        assert tool_names.count("guarded_echo") == 2

    async def test_multi_approval_with_compaction(self, make_pipeline_ctx):
        """Multiple guarded tools with compaction active — no duplicates."""
        from chrys.service.profiles.agents.schema import CompactionConfig

        big_result_text = "x" * 500

        from typing import Annotated

        from chrys.kernel import FunctionTool

        def _big_echo(message: Annotated[str, "Message"]) -> str:
            return f"big: {big_result_text}"

        big_echo = FunctionTool(func=_big_echo, name="big_echo", description="Big echo")
        guarded = FunctionTool(func=_big_echo, name="guarded_big", description="Guarded big")

        responses = [
            MockResponse(tool_calls=[("big_echo", "t1a", {"message": "fill1"})]),
            MockResponse(tool_calls=[("big_echo", "t1b", {"message": "fill2"})]),
            MockResponse(tool_calls=[("big_echo", "t1c", {"message": "fill3"})]),
            MockResponse(text="Turn 1 done"),
            MockResponse(tool_calls=[("big_echo", "t2e1", {"message": "read1"})]),
            MockResponse(tool_calls=[("guarded_big", "t2g1", {"message": "write1"})]),
            MockResponse(tool_calls=[("big_echo", "t2e2", {"message": "read2"})]),
            MockResponse(tool_calls=[("guarded_big", "t2g2", {"message": "write2"})]),
            MockResponse(text="Turn 2 done"),
        ]

        ctx = await make_pipeline_ctx(
            responses,
            approval_default="auto",
            tools=[big_echo, guarded],
            compaction=CompactionConfig(),
            max_context_tokens=2000,
        )
        await ctx.send_message("Fill context")
        await ctx.send_message("Multi-approval under compaction")

        raw = await ctx.get_session_messages()
        call_ids = extract_session_call_ids(raw)
        # No duplicate call ids regardless of whether compaction excluded
        # messages — duplicates would still surface as repeated ids.
        assert len(call_ids) == len(set(call_ids)), f"Duplicate call_ids: {call_ids}"
        # Phase 4 drop-all may have excluded tool_call messages from the
        # visible session view; surviving names are a subset of what was
        # attempted, and no unexpected names should appear.
        tool_names = extract_session_tool_names(raw)
        assert set(tool_names).issubset({"big_echo", "guarded_big"})

    async def test_multi_approval_streaming(self, make_pipeline_ctx):
        """Multiple guarded tools with streaming — same correctness guarantees."""
        responses = [
            MockResponse(tool_calls=[("echo", "e1", {"message": "read1"})]),
            MockResponse(tool_calls=[("guarded_echo", "g1", {"message": "write1"})]),
            MockResponse(tool_calls=[("echo", "e2", {"message": "read2"})]),
            MockResponse(tool_calls=[("guarded_echo", "g2", {"message": "write2"})]),
            MockResponse(text="Streaming done"),
        ]
        ctx = await make_pipeline_ctx(responses, approval_default="auto", stream=True)
        await ctx.send_message("Stream with guarded tools")

        raw = await ctx.get_session_messages()
        tool_names = extract_session_tool_names(raw)
        call_ids = extract_session_call_ids(raw)

        assert tool_names.count("echo") == 2
        assert tool_names.count("guarded_echo") == 2
        assert len(call_ids) == len(set(call_ids)), f"Duplicate call_ids: {call_ids}"


# ---------------------------------------------------------------------------
# S10: Compression + approval rollback
# ---------------------------------------------------------------------------


class TestCompressionWithApproval:
    """Compression (compress_context) followed by a guarded tool in the same
    turn.  With inline approval via ``ApprovalMiddleware``, there is no
    snapshot/restore — the compression is permanent and the guarded tool
    executes (or is rejected) inline.

    Verifies state consistency: blocks match summaries, no stale flags.
    """

    @pytest.mark.parametrize(
        ("stream_kwargs", "approve"),
        [({}, True), ({"stream": True}, True), ({}, False)],
        ids=["auto", "auto_streaming", "rejected"],
    )
    async def test_compression_then_guarded_tool(self, make_pipeline_ctx, stream_kwargs, approve):
        """compress_context in LLM response 1, guarded_echo in response 2.

        Auto-approved (buffered and streaming) the guarded tool runs; rejected,
        the LLM gets the refusal and answers with text.  Either way the
        compression is permanent, so the compressed blocks must keep matching
        the summaries left in history.

        Only the streaming row asks for a stream; the other two omit the
        argument so they keep exercising the engine factory's default mode.
        """
        final_text = "Done after compress + guarded" if approve else "OK, skipping that tool."
        responses = [
            MockResponse(text="R1"),
            MockResponse(text="R2"),
            MockResponse(text="R3"),
            MockResponse(
                tool_calls=[
                    ("compress_context", "cc1", {"marker_id": "turn_2", "summary": "Turns 1-2 summary"}),
                ]
            ),
            MockResponse(
                tool_calls=[
                    ("guarded_echo", "g1", {"message": "after compress" if approve else "will be rejected"}),
                ]
            ),
            MockResponse(text=final_text),
        ]
        approval = {"approval_default": "auto"} if approve else {"approval_overrides": {"guarded_echo": "require"}}
        ctx = await make_pipeline_ctx(responses, **stream_kwargs, **approval)
        for i in range(3):
            await ctx.send_message(f"turn {i + 1}")

        if approve:
            await ctx.send_message("compress and guard")
        else:
            await ctx.bus.publish(UserMessage(text="compress and guard"))
            approval_events = await wait_for_event(ctx.events, ApprovalRequest, timeout=20.0)
            await ctx.reject(approval_events[0].request_id)
            await wait_for_idle(ctx)

        state = ctx.engine.current.loaded.bindings.backend.history_state
        compressed = state.get("compressed_msgs", [])
        messages = state.get("messages", [])

        from chrys.service.context.providers.history import _is_compressed_summary

        summaries = [m for m in messages if _is_compressed_summary(m)]
        assert len(compressed) == len(summaries), (
            f"Inconsistent state: {len(compressed)} blocks but {len(summaries)} summaries"
        )

        raw = await ctx.get_session_messages()
        finals = extract_final_messages(ctx.events)
        if approve:
            assert "guarded_echo" in extract_session_tool_names(raw)
            assert any("Done" in f for f in finals)
        else:
            approvals = extract_session_approvals(raw)
            rejected = [a for a in approvals if a and a.get("status") == "user_rejected"]
            assert len(rejected) >= 1
            assert any("skipping" in f.lower() or "OK" in f for f in finals)
