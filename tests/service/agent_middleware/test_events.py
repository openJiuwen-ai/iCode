# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the events middleware package: tool events, hook dispatch, batch numbering, sub-agent events."""

from __future__ import annotations

import asyncio
import sys
import textwrap
from collections import UserDict
from types import SimpleNamespace

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    InvocationToolCallResult,
    InvocationToolCallStart,
    ShellToBackground,
)
from chrys.foundation.io.result_content import extract_result_text
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_call_context import TOOL_CALL_CONTEXT_METADATA_KEY, set_tool_context_builder
from chrys.foundation.tool_kinds import KIND_SHELL, KIND_SUB_AGENT
from chrys.foundation.tool_result_metadata import (
    SHELL_EXIT_CODE_METADATA_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_ERROR_MESSAGE_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
    TOOL_RESULT_METADATA_KEY,
)
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory_timing import TRAJECTORY_TIMING_KEY
from chrys.foundation.util.sub_agent_context import (
    SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY,
    sub_agent_parent_call_id,
    sub_agent_parent_result_metadata,
)
from chrys.kernel import Content, FunctionInvocationContext, FunctionTool
from chrys.kernel.exceptions import ModelVisibleToolError
from chrys.service.agent_middleware import (
    _APPROVAL_MODIFIED_ARGS_KEY,
    ResponseBatchCounter,
    SubAgentEventMiddleware,
    SubAgentStatsMiddleware,
    ToolEventMiddleware,
)
from chrys.service.agent_middleware._metadata_keys import (
    _APPROVAL_REJECTED_KEY,
    _REJECTION_MESSAGE_KEY,
    _REJECTION_SOURCE_KEY,
    _TOOL_INVOCATION_ORDER_KEY,
)
from chrys.service.agent_middleware.events.hook_dispatch import (
    append_extra_context_to_result,
    fire_after_tool_hooks,
    get_tool_invocation_order,
)
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.schema import HookConfig, HookDecision, HookExecution, HookMatch, HookRun, HooksFile
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.session.sub_agent_logs import SubAgentLogStats
from chrys.service.tools.builtins.shell import shell_background_request, shell_result_metadata
from chrys.service.tools.result_metadata import record_tool_success, tool_error
from tests.service.agent_middleware._event_fakes import (
    DenyBeforeToolHookManager,
    DualDecisionHookManager,
    _ctx,
)
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.event_capture import capture_event_sequence, capture_events

# ──────────────── ResponseBatchCounter ──────────────────────────────────


def test_batch_counter_starts_at_zero_and_numbers_each_response() -> None:
    counter = ResponseBatchCounter()
    assert counter.batch_id == 0
    counter.new_batch()
    assert counter.batch_id == 1
    counter.new_batch()
    assert counter.batch_id == 2


# ──────────────── ToolEventMiddleware — CancelledError handling ────────


async def test_tool_event_middleware_cancelled_no_result_event() -> None:
    """CancelledError during tool execution must not publish ToolCallResult.

    When the agent task is cancelled by user interrupt, ToolEventMiddleware
    should skip ToolCallResult publishing so the TUI's cancel_running_tools()
    is the sole authority on widget state.
    """

    bus = EventBus()
    mw = ToolEventMiddleware(bus, session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))

    # Collect published events
    events: list = []

    async def _on_start(e: InvocationToolCallStart) -> None:
        events.append(("start", e))

    async def _on_result(e: InvocationToolCallResult) -> None:
        events.append(("result", e))

    await bus.subscribe(InvocationToolCallStart, _on_start)
    await bus.subscribe(InvocationToolCallResult, _on_result)

    async def call_next_cancelled() -> None:
        raise asyncio.CancelledError()

    ctx = SimpleNamespace(
        function=SimpleNamespace(name="test_tool", chrys_kind=None),
        arguments={"arg": "val"},
        result=None,
        metadata=None,
    )

    with pytest.raises(asyncio.CancelledError):
        await mw.process(ctx, call_next_cancelled)

    # ToolCallStart should be published, but ToolCallResult should NOT
    start_events = [e for e in events if e[0] == "start"]
    result_events = [e for e in events if e[0] == "result"]
    assert len(start_events) == 1
    assert len(result_events) == 0
    timing = ctx.metadata[TRAJECTORY_TIMING_KEY]
    assert timing["started_at"] <= timing["finished_at"]
    assert timing["duration_ms"] >= 0


async def test_tool_event_middleware_sets_invocation_order_before_awaited_start_handler() -> None:
    """Parallel tool calls keep function-call order even if the first start publish blocks."""

    bus = EventBus()
    mw = ToolEventMiddleware(bus, session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))
    first_start_seen = asyncio.Event()
    release_first_start = asyncio.Event()

    async def _on_start(event: InvocationToolCallStart) -> None:
        if event.tool_name == "first":
            first_start_seen.set()
            await release_first_start.wait()

    await bus.subscribe(InvocationToolCallStart, _on_start)

    async def _next() -> None:
        return None

    first_ctx = _ctx("first")
    second_ctx = _ctx("second")

    first_task = asyncio.create_task(mw.process(first_ctx, _next))
    await first_start_seen.wait()
    second_task = asyncio.create_task(mw.process(second_ctx, _next))
    await asyncio.sleep(0)

    assert get_tool_invocation_order(first_ctx) == 0
    assert get_tool_invocation_order(second_ctx) == 1

    release_first_start.set()
    await asyncio.gather(first_task, second_task)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(7, 7), (0, 0), (-2, -2), ("12", 12), (" 7 ", 7), ("bad", None), (None, None), (True, None), (False, None)],
)
def test_tool_invocation_order_reads_ints_and_rejects_bools(raw: object, expected: int | None) -> None:
    order = get_tool_invocation_order(_ctx("tool", metadata={_TOOL_INVOCATION_ORDER_KEY: raw}))

    assert order == expected and type(order) is type(expected)


def test_tool_invocation_order_needs_a_dict_with_the_key() -> None:
    assert get_tool_invocation_order(_ctx("tool")) is None
    ctx = _ctx("tool")
    ctx.metadata = UserDict({_TOOL_INVOCATION_ORDER_KEY: 7})
    assert get_tool_invocation_order(ctx) is None


async def test_tool_event_middleware_uses_approval_modified_args_for_start_event() -> None:
    bus = EventBus()
    mw = ToolEventMiddleware(bus, session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))
    starts = await capture_events(bus, InvocationToolCallStart)

    async def _next() -> None:
        return None

    ctx = _ctx(
        "explore_agent", "sub_agent", args={"prompt": "old"}, metadata={_APPROVAL_MODIFIED_ARGS_KEY: {"prompt": "new"}}
    )

    await mw.process(ctx, _next)

    assert len(starts) == 1
    assert starts[0].args == {"prompt": "new"}


@pytest.mark.parametrize("transcript_final_text", ["unpublished final", ""])
async def test_tool_event_middleware_sub_agent_metadata_uses_provider_and_event_ids(
    transcript_final_text: str,
) -> None:
    bus = EventBus()
    mw = ToolEventMiddleware(bus, session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))
    results = await capture_events(bus, InvocationToolCallResult)

    async def _next() -> None:
        holder = sub_agent_parent_result_metadata.get()
        assert holder is not None
        assert holder.parent_provider_call_id == "provider-call"
        assert holder.parent_event_call_id
        assert holder.parent_event_call_id != holder.parent_provider_call_id
        holder.sub_agent_invocation_id = "a1b2c3d4e5f6"
        holder.sub_agent_log_file = "Explore_a1b2c3d4e5f6.json"
        holder.sub_agent_audit_complete = True
        holder.transcript_final_text = transcript_final_text
        record_tool_success()
        ctx.result = "done"

    ctx = _ctx("Explore", "sub_agent", args={"prompt": "inspect"}, metadata={"call_id": "provider-call"})

    await mw.process(ctx, _next)

    assert len(results) == 1
    assert results[0].call_id != "provider-call"
    assert results[0].metadata[TOOL_FAILED_METADATA_KEY] is False
    assert results[0].metadata["sub_agent_invocation_id"] == "a1b2c3d4e5f6"
    assert results[0].metadata["sub_agent_log_file"] == "Explore_a1b2c3d4e5f6.json"
    assert results[0].metadata["sub_agent_audit_complete"] is True
    assert results[0].metadata[SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY] == transcript_final_text
    assert ctx.metadata[TOOL_RESULT_METADATA_KEY] == {
        TOOL_FAILED_METADATA_KEY: False,
        "sub_agent_invocation_id": "a1b2c3d4e5f6",
        "sub_agent_log_file": "Explore_a1b2c3d4e5f6.json",
        "sub_agent_audit_complete": True,
    }


@pytest.mark.parametrize("transcript_final_text", ["unpublished final", ""])
async def test_sub_agent_event_middleware_binds_a_nested_delegation_like_the_chat_turn(
    transcript_final_text: str,
) -> None:
    """A workflow node (or a nested sub-agent) delegating to a child binds it through the same
    ContextVars as ToolEventMiddleware, and folds the child's ids into the result the same way."""
    bus = EventBus()
    node = InvocationOrigin("workflow_node", "test", "node-run", None)
    mw = SubAgentEventMiddleware(bus, agent_name="Node", invocation_id="node-run", session_id="test", origin=node)
    results = await capture_events(bus, InvocationToolCallResult)
    starts = await capture_events(bus, InvocationToolCallStart)
    seen_parent_call_ids: list[str] = []

    async def _next() -> None:
        holder = sub_agent_parent_result_metadata.get()
        assert holder is not None
        assert holder.parent_provider_call_id == "provider-call"
        assert holder.parent_event_call_id == sub_agent_parent_call_id.get()
        assert holder.parent_event_call_id != holder.parent_provider_call_id
        assert holder.commit_interrupted_result is None
        seen_parent_call_ids.append(holder.parent_event_call_id)
        holder.sub_agent_invocation_id = "a1b2c3d4e5f6"
        holder.sub_agent_log_file = "Child_a1b2c3d4e5f6.json"
        holder.sub_agent_audit_complete = True
        holder.transcript_final_text = transcript_final_text
        record_tool_success()
        ctx.result = "done"

    ctx = _ctx("Child", KIND_SUB_AGENT, args={"prompt": "inspect"}, metadata={"call_id": "provider-call"})

    await mw.process(ctx, _next)

    # The binding is scoped to the call: nothing leaks to the next tool of the same agent.
    assert sub_agent_parent_call_id.get() == ""
    assert sub_agent_parent_result_metadata.get() is None
    assert [event.call_id for event in starts] == seen_parent_call_ids
    assert len(results) == 1
    assert results[0].call_id == seen_parent_call_ids[0] != "provider-call"
    assert results[0].metadata[TOOL_FAILED_METADATA_KEY] is False
    assert results[0].metadata["sub_agent_invocation_id"] == "a1b2c3d4e5f6"
    assert results[0].metadata["sub_agent_log_file"] == "Child_a1b2c3d4e5f6.json"
    assert results[0].metadata["sub_agent_audit_complete"] is True
    assert results[0].metadata[SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY] == transcript_final_text
    assert ctx.metadata[TOOL_RESULT_METADATA_KEY] == {
        TOOL_FAILED_METADATA_KEY: False,
        "sub_agent_invocation_id": "a1b2c3d4e5f6",
        "sub_agent_log_file": "Child_a1b2c3d4e5f6.json",
        "sub_agent_audit_complete": True,
    }


async def test_sub_agent_event_middleware_leaves_ordinary_tools_unbound() -> None:
    mw = SubAgentEventMiddleware(
        EventBus(), agent_name="Node", invocation_id="node-run", origin=InvocationOrigin("workflow_node", "", "n", None)
    )
    observed: list[object] = []

    async def _next() -> None:
        observed.append(sub_agent_parent_call_id.get())
        observed.append(sub_agent_parent_result_metadata.get())
        record_tool_success()
        ctx.result = "done"

    ctx = _ctx("read_file", "filesystem.read", args={"path": "README.md"}, metadata={"call_id": "provider-call"})

    await mw.process(ctx, _next)

    assert observed == ["", None]
    assert ctx.metadata[TOOL_RESULT_METADATA_KEY] == {TOOL_FAILED_METADATA_KEY: False}


@pytest.mark.parametrize("exit_code", [0, 42])
async def test_tool_event_middleware_publishes_and_persists_shell_exit_metadata(exit_code: int) -> None:
    """Shell backend metadata should survive ToolCallResult and replay persistence."""

    bus = EventBus()
    mw = ToolEventMiddleware(bus, session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))
    results = await capture_events(bus, InvocationToolCallResult)

    async def _next() -> None:
        metadata = shell_result_metadata.get()
        assert metadata is not None
        metadata[SHELL_EXIT_CODE_METADATA_KEY] = exit_code
        ctx.result = f"boom\n[exit_code: {exit_code}]"

    ctx = _ctx(
        "bash",
        KIND_SHELL,
        args={"command": "sleep 999"},
        metadata={"call_id": "provider-shell-call", _TOOL_INVOCATION_ORDER_KEY: 3},
    )

    await mw.process(ctx, _next)

    assert results[0].metadata[SHELL_EXIT_CODE_METADATA_KEY] == exit_code
    # The upstream-seeded invocation ordinal is honored, and the persistable
    # subset rides the invocation context for the kernel fold.
    assert ctx.metadata[_TOOL_INVOCATION_ORDER_KEY] == 3
    assert ctx.metadata[TOOL_RESULT_METADATA_KEY] == {SHELL_EXIT_CODE_METADATA_KEY: exit_code}


@pytest.mark.parametrize("main_agent", [True, False])
async def test_only_a_main_agent_shell_call_moves_to_the_background_when_the_user_asks(main_agent: bool) -> None:
    bus = EventBus()
    turn = InvocationOrigin("turn", "test", "turn-test", None)
    origin = turn if main_agent else InvocationOrigin("sub_agent", "test", "child", turn)
    mw = ToolEventMiddleware(bus, session_id="test", origin=origin)
    asked: list[bool] = []

    async def _next() -> None:
        request = shell_background_request.get()
        asked.append(request is not None and request.is_set())
        await bus.publish(ShellToBackground(session_id="test"))
        asked.append(request is not None and request.is_set())
        ctx.result = "[exit_code: 0]"

    ctx = _ctx("bash", KIND_SHELL, args={"command": "npm run dev"}, metadata={"call_id": "provider-shell-call"})

    await mw.process(ctx, _next)

    assert asked == [False, main_agent]
    assert shell_background_request.get() is None
    assert not bus._handlers.get(ShellToBackground)


async def test_tool_event_middleware_publishes_and_persists_generic_failure_metadata() -> None:
    bus = EventBus()
    mw = ToolEventMiddleware(bus, session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))
    results = await capture_events(bus, InvocationToolCallResult)

    async def _next() -> None:
        ctx.result = tool_error("validation", "bad input")

    ctx = _ctx("read_file", "filesystem.read", args={"path": "missing.txt"})

    await mw.process(ctx, _next)

    assert results[0].result == "Error: bad input"
    assert results[0].metadata[TOOL_FAILED_METADATA_KEY] is True
    assert results[0].metadata[TOOL_ERROR_KIND_METADATA_KEY] == "validation"
    assert results[0].metadata[TOOL_ERROR_MESSAGE_METADATA_KEY] == "bad input"
    carried = ctx.metadata[TOOL_RESULT_METADATA_KEY]
    assert carried[TOOL_FAILED_METADATA_KEY] is True
    assert carried[TOOL_ERROR_KIND_METADATA_KEY] == "validation"


async def test_tool_event_middleware_hook_denial_is_not_public_approval_rejection() -> None:
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    manager = DenyBeforeToolHookManager(
        {
            HookEvent.BEFORE_TOOL_CALL: HookDecision(blocked=True, block_reason="blocked by policy"),
            HookEvent.AFTER_TOOL_CALL: HookDecision(),
        }
    )
    mw = ToolEventMiddleware(
        bus,
        session_id="test",
        hook_manager=manager,
        profile_name="Code",
        origin=InvocationOrigin("turn", "test", "turn-test", None),
    )
    ctx = _ctx("write_file", "filesystem.write", args={"path": "a.txt", "content": "x"})

    async def _next() -> None:
        raise AssertionError("blocked hook should prevent the tool body")

    await mw.process(ctx, _next)

    assert results[0].result == "Error: blocked by policy"
    assert results[0].metadata[TOOL_FAILED_METADATA_KEY] is True
    assert results[0].metadata[TOOL_ERROR_KIND_METADATA_KEY] == "hook_denied"
    assert results[0].metadata[TOOL_ERROR_MESSAGE_METADATA_KEY] == "blocked by policy"
    assert "approval" not in results[0].metadata
    assert manager.after_payloads[0]["result"] == {
        "text": "Error: blocked by policy",
        "duration_ms": 0,
        "error": False,
        "failed": True,
        "approval_rejected": True,
        "rejection_source": "hook",
        "hook_denied": True,
    }


async def test_tool_event_middleware_user_rejection_public_metadata_is_persisted() -> None:
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    mw = ToolEventMiddleware(bus, session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))
    ctx = _ctx(
        "write_file",
        "filesystem.write",
        args={"path": "a.txt", "content": "x"},
        metadata={
            "call_id": "provider-write-call",
            _APPROVAL_REJECTED_KEY: True,
            _REJECTION_SOURCE_KEY: "user",
            _REJECTION_MESSAGE_KEY: "Denied by operator",
            _TOOL_INVOCATION_ORDER_KEY: 7,
        },
    )

    async def _next() -> None:
        ctx.result = "Error: Denied by operator"

    await mw.process(ctx, _next)

    assert results[0].metadata[TOOL_FAILED_METADATA_KEY] is True
    assert results[0].metadata["approval"] == "user_rejected"
    assert results[0].metadata[TOOL_ERROR_KIND_METADATA_KEY] == "approval_rejected"
    assert results[0].metadata[TOOL_ERROR_MESSAGE_METADATA_KEY] == "Denied by operator"
    assert ctx.metadata[_TOOL_INVOCATION_ORDER_KEY] == 7
    assert ctx.metadata[TOOL_RESULT_METADATA_KEY] == {
        TOOL_FAILED_METADATA_KEY: True,
        "approval": "user_rejected",
        TOOL_ERROR_KIND_METADATA_KEY: "approval_rejected",
        TOOL_ERROR_MESSAGE_METADATA_KEY: "Denied by operator",
    }


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        ("prejudge", "prejudge_blocked"),
        ("approval_invalidated", "approval_invalidated"),
    ],
)
async def test_structured_approval_rejection_is_persisted_without_impersonating_user_or_hook(
    source: str, expected_kind: str
) -> None:
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    mw = ToolEventMiddleware(bus, session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))
    ctx = _ctx(
        "bash",
        KIND_SHELL,
        args={"command": "rm sample"},
        metadata={
            _APPROVAL_REJECTED_KEY: True,
            _REJECTION_SOURCE_KEY: source,
            _REJECTION_MESSAGE_KEY: "Denied by structured pre-judge",
        },
    )

    async def _next() -> None:
        ctx.result = "Error: Denied by structured pre-judge"

    await mw.process(ctx, _next)

    assert results[0].metadata[TOOL_FAILED_METADATA_KEY] is True
    assert results[0].metadata[TOOL_ERROR_KIND_METADATA_KEY] == expected_kind
    assert "approval" not in results[0].metadata
    assert ctx.metadata[TOOL_RESULT_METADATA_KEY][TOOL_ERROR_KIND_METADATA_KEY] == expected_kind


async def test_sub_agent_stats_middleware_counts_completed_tools_without_event_bus() -> None:
    stats = SubAgentLogStats()
    mw = SubAgentStatsMiddleware(stats)
    ctx = SimpleNamespace(function=SimpleNamespace(name="read_file"), arguments={}, result=None, metadata={})

    async def _ok() -> None:
        ctx.result = "done"

    await mw.process(ctx, _ok)

    assert stats.tool_call_count == 1

    async def _cancel() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await mw.process(ctx, _cancel)
    assert stats.tool_call_count == 1


async def test_tool_event_middleware_exception_publishes_result() -> None:
    """Regular exceptions should still publish ToolCallResult with error text."""

    bus = EventBus()
    mw = ToolEventMiddleware(bus, session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))

    events: list = []

    async def _on_start(e: InvocationToolCallStart) -> None:
        events.append(("start", e))

    async def _on_result(e: InvocationToolCallResult) -> None:
        events.append(("result", e))

    await bus.subscribe(InvocationToolCallStart, _on_start)
    await bus.subscribe(InvocationToolCallResult, _on_result)

    async def call_next_error() -> None:
        raise ValueError("tool failed")

    ctx = SimpleNamespace(
        function=SimpleNamespace(name="test_tool", chrys_kind=None),
        arguments={},
        result=None,
        metadata=None,
    )

    with pytest.raises(ValueError, match="tool failed"):
        await mw.process(ctx, call_next_error)

    result_events = [e for e in events if e[0] == "result"]
    assert len(result_events) == 1
    assert "tool failed" in result_events[0][1].result
    assert result_events[0][1].metadata["errored"] is True


async def test_tool_event_middleware_empty_exception_message_publishes_readable_error() -> None:
    """Transport exceptions with blank ``str(exc)`` still need visible tool output."""

    class ReadTimeout(Exception):
        pass

    bus = EventBus()
    mw = ToolEventMiddleware(bus, session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))
    results = await capture_events(bus, InvocationToolCallResult)

    async def call_next_error() -> None:
        raise ReadTimeout(TimeoutError())

    ctx = SimpleNamespace(
        function=SimpleNamespace(name="test_tool", chrys_kind=None),
        arguments={},
        result=None,
        metadata=None,
    )

    with pytest.raises(ReadTimeout):
        await mw.process(ctx, call_next_error)

    assert len(results) == 1
    assert results[0].result == "Error: Read timed out (ReadTimeout)"


async def test_tool_card_shows_the_model_visible_error_the_model_read() -> None:
    """The card shows the message the model read, not the cause that message was written to replace."""
    bus = EventBus()
    mw = ToolEventMiddleware(bus, session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))
    results = await capture_events(bus, InvocationToolCallResult)
    clash = ValueError("Duplicate tool name 'remote'. Tool names must be unique.")
    raised = ModelVisibleToolError("Cannot load 'remote': another tool already has that name.", inner_exception=clash)
    raised.__cause__ = clash

    async def call_next_error() -> None:
        raise raised

    with pytest.raises(ModelVisibleToolError):
        await mw.process(FunctionInvocationContext(FunctionTool(name="test_tool"), {}), call_next_error)

    assert [result.result for result in results] == ["Error: Cannot load 'remote': another tool already has that name."]


async def test_tool_event_middleware_hooks_modify_args_before_start_event(tmp_path) -> None:
    """Hook-modified args should be what UI events and the tool body see."""

    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "modify", "args_override": {"path": "/tmp/safer"}}, f)
        """
    )
    manager = HookManager(
        file=HooksFile(
            hooks=[
                HookConfig(
                    id="rewrite",
                    event=HookEvent.BEFORE_TOOL_CALL,
                    run=HookRun(type="command", argv=[sys.executable, "-c", script]),
                    execution=HookExecution(mode="blocking"),
                    match=HookMatch(tool_name="write_file"),
                )
            ]
        ),
        hooks_dir=tmp_path / "hooks",
    )
    bus = EventBus()
    starts = await capture_events(bus, InvocationToolCallStart)
    mw = ToolEventMiddleware(
        bus,
        session_id="s1",
        hook_manager=manager,
        profile_name="Code",
        origin=InvocationOrigin("turn", "s1", "turn-test", None),
    )
    ctx = _ctx("write_file", "filesystem.write", args={"path": "/tmp/original", "content": "x"})

    async def call_next() -> None:
        assert ctx.arguments["path"] == "/tmp/safer"
        ctx.result = "ok"

    await mw.process(ctx, call_next)

    assert ctx.arguments["path"] == "/tmp/safer"
    assert ctx.arguments["content"] == "x"
    assert starts[0].args["path"] == "/tmp/safer"
    assert starts[0].args["content"] == "x"


async def test_tool_event_middleware_file_tool_missing_path_does_not_keyerror(tmp_path) -> None:
    """Hook rewrites can remove a usable lock path; middleware should still emit events."""

    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "modify", "args_override": {"path": None}}, f)
        """
    )
    manager = HookManager(
        file=HooksFile(
            hooks=[
                HookConfig(
                    id="remove-path",
                    event=HookEvent.BEFORE_TOOL_CALL,
                    run=HookRun(type="command", argv=[sys.executable, "-c", script]),
                    execution=HookExecution(mode="blocking"),
                    match=HookMatch(tool_name="write_file"),
                )
            ]
        ),
        hooks_dir=tmp_path / "hooks",
    )
    mw = ToolEventMiddleware(
        EventBus(),
        session_id="s1",
        hook_manager=manager,
        profile_name="Code",
        origin=InvocationOrigin("turn", "s1", "turn-test", None),
    )
    ctx = _ctx("write_file", "filesystem.write", args={"path": "/tmp/original", "content": "x"})

    async def call_next() -> None:
        ctx.result = "ok"

    await mw.process(ctx, call_next)
    assert ctx.arguments["path"] is None


async def test_tool_event_middleware_after_hook_extra_context_appends_to_text_result(tmp_path) -> None:
    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"extra_context": "hook note"}, f)
        """
    )
    manager = HookManager(
        file=HooksFile(
            hooks=[
                HookConfig(
                    id="note",
                    event=HookEvent.AFTER_TOOL_CALL,
                    run=HookRun(type="command", argv=[sys.executable, "-c", script]),
                    execution=HookExecution(mode="blocking"),
                )
            ]
        ),
        hooks_dir=tmp_path / "hooks",
    )
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    mw = ToolEventMiddleware(
        bus,
        session_id="s1",
        hook_manager=manager,
        profile_name="Code",
        origin=InvocationOrigin("turn", "s1", "turn-test", None),
    )
    ctx = _ctx("read_file", "filesystem.read", args={"path": "/tmp/x"})

    async def call_next() -> None:
        ctx.result = "base result"

    await mw.process(ctx, call_next)

    assert ctx.result == "base result\n\nhook note"
    assert results[0].result == "base result\n\nhook note"


async def test_failed_tool_call_merges_after_and_error_hook_decisions() -> None:
    manager = DualDecisionHookManager(
        {
            HookEvent.AFTER_TOOL_CALL: HookDecision(
                blocked=True,
                block_reason="after blocked",
                args_override={"shared": "after", "after": 1},
                system_reminders=["after reminder"],
                extra_context=["after context"],
            ),
            HookEvent.TOOL_ERROR: HookDecision(
                blocked=False,
                block_reason="error did not block",
                args_override={"shared": "error", "error": 2},
                system_reminders=["error reminder"],
                extra_context=["error context"],
            ),
        }
    )
    decision = await fire_after_tool_hooks(
        manager=manager,  # type: ignore[arg-type]
        session_id="s1",
        profile_name="Code",
        tool_name="read_file",
        kind="filesystem.read",
        call_id="call-1",
        args={"path": "/tmp/x"},
        result_text="Error: failed",
        duration_ms=12,
        errored=True,
        failed=True,
        approval_rejected=False,
        rejection_source="",
        workspace_cwd="/tmp",
    )

    assert manager.events == [HookEvent.AFTER_TOOL_CALL, HookEvent.TOOL_ERROR]
    assert decision.blocked is True
    assert decision.block_reason == "after blocked"
    assert decision.args_override == {"shared": "error", "after": 1, "error": 2}
    assert decision.system_reminders == ["after reminder", "error reminder"]
    assert decision.extra_context == ["after context", "error context"]


def test_append_extra_context_updates_content_list_result() -> None:
    result, result_text = append_extra_context_to_result(
        [Content.from_text("base result")],
        "base result",
        ["hook note"],
    )

    assert result_text == "base result\n\nhook note"
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[1].text == "\nhook note"
    assert extract_result_text(result) == result_text


# ──────────────── ToolEventMiddleware — implicit mutation windows ─────────


async def test_tool_event_middleware_serializes_implicit_windows(tmp_path) -> None:
    """Concurrent shell calls should not overlap their before/after mutation windows."""

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    tracker = MutationTracker(SnapshotStore(session_dir))
    tracker.start_turn(1)
    mw = ToolEventMiddleware(
        EventBus(),
        session_id="s1",
        mutation_tracker=tracker,
        workspace_cwd=str(tmp_path),
        serialize_implicit_windows=True,
        origin=InvocationOrigin("turn", "s1", "turn-test", None),
    )

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    active = 0
    max_active = 0

    async def first_next() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        first_entered.set()
        await release_first.wait()
        active -= 1

    async def second_next() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        second_entered.set()
        active -= 1

    first_ctx = _ctx("bash", KIND_SHELL, args={"command": "true"})
    second_ctx = _ctx("bash", KIND_SHELL, args={"command": "true"})

    first_task = asyncio.create_task(mw.process(first_ctx, first_next))
    await first_entered.wait()
    second_task = asyncio.create_task(mw.process(second_ctx, second_next))
    await asyncio.sleep(0.05)

    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()
    assert max_active == 1


async def test_tool_event_middleware_runs_implicit_windows_in_parallel_by_default(tmp_path) -> None:
    """Shell calls keep the historical parallel behavior unless serialization is enabled."""

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    tracker = MutationTracker(SnapshotStore(session_dir))
    tracker.start_turn(1)
    mw = ToolEventMiddleware(
        EventBus(),
        session_id="s1",
        mutation_tracker=tracker,
        workspace_cwd=str(tmp_path),
        origin=InvocationOrigin("turn", "s1", "turn-test", None),
    )

    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_both = asyncio.Event()
    active = 0
    max_active = 0

    async def first_next() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        first_entered.set()
        await release_both.wait()
        active -= 1

    async def second_next() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        second_entered.set()
        await release_both.wait()
        active -= 1

    first_ctx = _ctx("bash", KIND_SHELL, args={"command": "true"})
    second_ctx = _ctx("bash", KIND_SHELL, args={"command": "true"})

    first_task = asyncio.create_task(mw.process(first_ctx, first_next))
    await first_entered.wait()
    second_task = asyncio.create_task(mw.process(second_ctx, second_next))
    await asyncio.wait_for(second_entered.wait(), timeout=0.5)

    release_both.set()
    await asyncio.gather(first_task, second_task)
    assert max_active == 2


# ──────────────── provenance context carriage on the invocation context ────


def _provenance_function(name: str = "load_skill") -> SimpleNamespace:
    fn = SimpleNamespace(name=name, chrys_kind="skill")
    set_tool_context_builder(fn, lambda args: {"skill_name": str(args.get("skill_name", "")).lower()})
    return fn


async def test_tool_event_middleware_carries_context_built_from_final_args() -> None:
    """The builder must see approval-modified args, not the stale captured ones."""

    mw = ToolEventMiddleware(EventBus(), session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))
    ctx = SimpleNamespace(
        function=_provenance_function(),
        arguments={"skill_name": "STALE"},
        result=None,
        metadata={_APPROVAL_MODIFIED_ARGS_KEY: {"skill_name": "MODIFIED"}},
    )

    async def _next() -> None:
        ctx.result = "ok"

    await mw.process(ctx, _next)

    assert ctx.metadata[TOOL_CALL_CONTEXT_METADATA_KEY] == {"skill_name": "modified"}


async def test_tool_event_middleware_rejected_call_still_carries_context() -> None:
    """Rejected calls never execute, but the model still sees the call — provenance must persist."""

    mw = ToolEventMiddleware(EventBus(), session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))
    ctx = SimpleNamespace(
        function=_provenance_function(),
        arguments={"skill_name": "PDF"},
        result="Rejected by user.",
        metadata={_APPROVAL_REJECTED_KEY: True, _REJECTION_SOURCE_KEY: "user"},
    )

    async def _blocked_next() -> None:
        return None

    await mw.process(ctx, _blocked_next)

    assert ctx.metadata[TOOL_CALL_CONTEXT_METADATA_KEY] == {"skill_name": "pdf"}


async def test_sub_agent_event_middleware_resolves_context_from_modified_args() -> None:
    """The shared final_tool_args helper fixes the sub-agent stale-args path too."""

    mw = SubAgentEventMiddleware(
        EventBus(),
        agent_name="Explore",
        invocation_id="inv123",
        origin=InvocationOrigin("sub_agent", "", "inv123", None),
    )
    ctx = SimpleNamespace(
        function=_provenance_function(),
        arguments={"skill_name": "STALE"},
        result=None,
        metadata={_APPROVAL_MODIFIED_ARGS_KEY: {"skill_name": "MODIFIED"}},
    )

    async def _next() -> None:
        ctx.result = "ok"

    await mw.process(ctx, _next)
    await mw.flush_progress()

    assert ctx.metadata[TOOL_CALL_CONTEXT_METADATA_KEY] == {"skill_name": "modified"}


@pytest.mark.parametrize("sub_agent", [False, True], ids=["main", "sub_agent"])
async def test_tool_call_publishes_its_card_and_no_model_text(sub_agent: bool) -> None:
    """Model text reaches frontends from the run's content projector, never from a tool start."""

    bus = EventBus()
    middleware = (
        SubAgentEventMiddleware(
            bus, agent_name="Explore", invocation_id="inv123", origin=InvocationOrigin("sub_agent", "", "inv123", None)
        )
        if sub_agent
        else ToolEventMiddleware(
            bus,
            session_id="test",
            batch_counter=ResponseBatchCounter(),
            origin=InvocationOrigin("turn", "test", "turn-test", None),
        )
    )
    context = _ctx("read_file", "filesystem.read", args={"path": "README.md"})

    async def _next() -> None:
        context.result = "done"

    async with capture_event_sequence(
        bus,
        InvocationMessage,
        InvocationContentDelta,
        InvocationContentFinished,
        InvocationToolCallStart,
        InvocationToolCallResult,
    ) as events:
        await middleware.process(context, _next)
        if isinstance(middleware, SubAgentEventMiddleware):
            await middleware.flush_progress()

    assert [type(event) for event in events] == [InvocationToolCallStart, InvocationToolCallResult]


# ──────────────── ToolEventMiddleware — batch records (§2.1.1) ─────────


def _batch_record_ctx(provider_call_id: str, tool_name: str = "echo") -> SimpleNamespace:
    return _ctx(tool_name, args={"message": "hi"}, metadata={"call_id": provider_call_id})


async def test_tool_event_middleware_stamps_each_call_with_the_counters_current_batch() -> None:
    """Batch records and the trajectory's ``batch_index`` both read the counter as each call dispatches."""

    sink = FakeSink()
    counter = ResponseBatchCounter()
    mw = ToolEventMiddleware(
        EventBus(),
        session_id="test",
        batch_counter=counter,
        origin=InvocationOrigin("turn", "test", "turn-test", None),
    )

    async def _next() -> None:
        return None

    with trajectory_scope(make_context(sink)):
        counter.new_batch()
        await mw.process(_batch_record_ctx("first"), _next)
        await mw.process(_batch_record_ctx("second"), _next)
        counter.new_batch()
        await mw.process(_batch_record_ctx("third"), _next)

    assert [(record.provider_call_id, record.batch_id) for record in mw.drain_batch_records()] == [
        ("first", 1),
        ("second", 1),
        ("third", 2),
    ]
    assert [draft.payload["batch_index"] for draft in sink.of_type(EventType.TOOL_OPERATION_STARTED)] == [1, 1, 2]


async def test_tool_event_middleware_without_a_counter_records_no_batch() -> None:
    sink = FakeSink()
    mw = ToolEventMiddleware(EventBus(), session_id="test", origin=InvocationOrigin("turn", "test", "turn-test", None))

    async def _next() -> None:
        return None

    with trajectory_scope(make_context(sink)):
        await mw.process(_batch_record_ctx("unbatched"), _next)

    assert mw.drain_batch_records() == []
    started = sink.only(EventType.TOOL_OPERATION_STARTED)
    assert "batch_index" not in started.payload


async def test_tool_event_middleware_mainline_batch_record_has_non_empty_provider_id() -> None:
    """POSITIVE producer pin: a normal middleware-dispatched call always records
    a non-empty provider_call_id — the persist-side drop-without-provider-id
    guard must never fire on the mainline path.
    """

    counter = ResponseBatchCounter()
    counter.new_batch()
    mw = ToolEventMiddleware(
        EventBus(),
        session_id="test",
        batch_counter=counter,
        origin=InvocationOrigin("turn", "test", "turn-test", None),
    )
    ctx = _batch_record_ctx("provider-batch-call")

    async def _next() -> None:
        ctx.result = "ok"

    await mw.process(ctx, _next)

    records = mw.drain_batch_records()
    assert len(records) == 1
    assert records[0].provider_call_id == "provider-batch-call"
    assert records[0].provider_call_id  # never empty on the mainline path
    assert records[0].tool_name == "echo"
    assert records[0].batch_id == 1
    # drain clears
    assert mw.drain_batch_records() == []


async def test_tool_event_middleware_records_batch_record_pre_execution() -> None:
    """A cancelled (interrupted) call still gets its record — recording happens
    before the tool runs, so no result/metadata is required.
    """

    counter = ResponseBatchCounter()
    counter.new_batch()
    mw = ToolEventMiddleware(
        EventBus(),
        session_id="test",
        batch_counter=counter,
        origin=InvocationOrigin("turn", "test", "turn-test", None),
    )
    ctx = _batch_record_ctx("provider-cancelled-call")

    async def _next() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await mw.process(ctx, _next)

    records = mw.drain_batch_records()
    assert len(records) == 1
    assert records[0].provider_call_id == "provider-cancelled-call"
    assert records[0].batch_id == 1


async def test_tool_event_middleware_metadata_less_call_still_records_batch_record() -> None:
    """A call with no persistable result metadata keeps its batch record —
    records must never be derived from the tool-result metadata stream.
    """

    counter = ResponseBatchCounter()
    counter.new_batch()
    mw = ToolEventMiddleware(
        EventBus(),
        session_id="test",
        batch_counter=counter,
        origin=InvocationOrigin("turn", "test", "turn-test", None),
    )
    ctx = _batch_record_ctx("provider-plain-call")

    async def _next() -> None:
        return None  # result stays None; nothing persistable

    await mw.process(ctx, _next)

    assert TOOL_RESULT_METADATA_KEY not in ctx.metadata
    records = mw.drain_batch_records()
    assert len(records) == 1
    assert records[0].provider_call_id == "provider-plain-call"


async def test_clear_batch_records_drops_rolled_back_attempt_records() -> None:
    """Retry rollback (restore_history_snapshot) clears records so a stale
    provider id can never stamp post-retry history.
    """

    counter = ResponseBatchCounter()
    counter.new_batch()
    mw = ToolEventMiddleware(
        EventBus(),
        session_id="test",
        batch_counter=counter,
        origin=InvocationOrigin("turn", "test", "turn-test", None),
    )
    ctx = _batch_record_ctx("provider-rolled-back-call")

    async def _next() -> None:
        ctx.result = "ok"

    await mw.process(ctx, _next)
    mw.clear_batch_records()

    assert mw.drain_batch_records() == []
