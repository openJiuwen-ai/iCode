# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for execution stamps and the TOOL_OPERATION lifecycle across both tool-event middlewares."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_call_context import set_tool_context, set_tool_context_builder
from chrys.foundation.tool_execution_stamp import (
    EFFECTIVE_ARGS_MAX_CHARS,
    EXECUTION_STAMP_KEY,
    build_execution_stamp,
)
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType, ToolOutcome
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.metadata import OPERATION_ID_KEY
from chrys.foundation.trajectory_timing import TRAJECTORY_TIMING_KEY
from chrys.kernel._result_ceiling import apply_result_ceiling
from chrys.service.agent_middleware import SubAgentEventMiddleware, ToolEventMiddleware
from chrys.service.agent_middleware._metadata_keys import _APPROVAL_REJECTED_KEY
from chrys.service.agent_middleware.events import sub_agent_events as sub_agent_events_module
from chrys.service.agent_middleware.events import tool_events as tool_events_module
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.schema import HookDecision
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.tools.result_metadata import tool_error
from tests.service.agent_middleware._event_fakes import RewriteArgsHookManager, _ctx
from tests.service.trajectory._fakes import CancelAckSink, FakeSink, make_context


def _execution_stamp_middleware(
    kind: str,
    *,
    tool_result_ceiling_tokens: int | None = None,
    mutation_tracker: MutationTracker | None = None,
    workspace_cwd: str = "",
):
    if kind == "main":
        return ToolEventMiddleware(
            EventBus(),
            session_id="stamp-test",
            tool_result_ceiling_tokens=tool_result_ceiling_tokens,
            mutation_tracker=mutation_tracker,
            workspace_cwd=workspace_cwd,
            origin=InvocationOrigin("turn", "stamp-test", "turn-test", None),
        )
    return SubAgentEventMiddleware(
        EventBus(),
        agent_name="Explore",
        invocation_id="inv-stamp",
        tool_result_ceiling_tokens=tool_result_ceiling_tokens,
        mutation_tracker=mutation_tracker,
        workspace_cwd=workspace_cwd,
        origin=InvocationOrigin("sub_agent", "", "inv-stamp", None),
    )


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_stamp_success(kind: str) -> None:
    middleware = _execution_stamp_middleware(kind)
    context = _ctx("echo", args={"b": 2, "a": 1})

    async def _next() -> None:
        context.result = "done"

    await middleware.process(context, _next)

    stamp = context.metadata[EXECUTION_STAMP_KEY]
    assert stamp["effective_args"] == '{"a":1,"b":2}'
    assert stamp["outcome"] == "ok"
    assert "error_kind" not in stamp
    timing = context.metadata[TRAJECTORY_TIMING_KEY]
    assert timing["started_at"] <= timing["finished_at"]
    assert isinstance(timing["duration_ms"], int)
    assert timing["duration_ms"] >= 0


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_stamp_rejection_as_instant(kind: str) -> None:
    middleware = _execution_stamp_middleware(kind)
    context = _ctx(
        "write_file",
        "filesystem.write",
        args={"path": "a.txt", "content": "x"},
        metadata={_APPROVAL_REJECTED_KEY: True},
    )

    async def _next() -> None:
        context.result = "Error: rejected"

    await middleware.process(context, _next)

    timing = context.metadata[TRAJECTORY_TIMING_KEY]
    assert timing["started_at"] == timing["finished_at"]
    assert timing["duration_ms"] == 0


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_record_the_wait_a_rejection_ended(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The card shows no duration for a call that never ran; the log keeps the wait."""
    module = tool_events_module if kind == "main" else sub_agent_events_module
    # Two reads: the start of the call itself and the finish. Replace the
    # module's own ``time`` name,
    # never the stdlib module: the event loop reads ``time.monotonic`` too, and
    # a fixed clock stalls it.
    clock = iter([100.0, 112.5])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    middleware = _execution_stamp_middleware(kind)
    context = _ctx(
        "write_file",
        "filesystem.write",
        args={"path": "a.txt", "content": "x"},
        metadata={_APPROVAL_REJECTED_KEY: True},
    )

    async def _next() -> None:
        context.result = "Error: rejected"

    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        await middleware.process(context, _next)

    assert context.metadata[TRAJECTORY_TIMING_KEY]["duration_ms"] == 0
    finished = sink.only(EventType.TOOL_OPERATION_FINISHED)
    assert finished.payload["outcome"] == ToolOutcome.REJECTED
    assert finished.payload["duration_ms"] == 12500


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_close_an_operation_abandoned_before_its_start_marker(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel counts the call dispatched before this middleware can open the
    operation, and the preprocessing in between — hooks, the mutation lock, the
    start event on the bus — is cancellable. An interrupt there must still leave
    the minted operation accounted for."""
    module = tool_events_module if kind == "main" else sub_agent_events_module

    async def _cancelled(**_kwargs: object) -> bool:
        raise asyncio.CancelledError

    monkeypatch.setattr(module, "apply_before_tool_hooks", _cancelled)
    middleware = _execution_stamp_middleware(kind)
    operation_id = new_analytics_id()
    context = _ctx(
        "write_file",
        "filesystem.write",
        args={"path": "a.txt", "content": "x"},
        metadata={OPERATION_ID_KEY: operation_id},
    )

    async def _next() -> None:
        context.result = "ok"

    sink = FakeSink()
    with trajectory_scope(make_context(sink)), pytest.raises(asyncio.CancelledError):
        await middleware.process(context, _next)

    tool_started = sink.only(EventType.TOOL_OPERATION_STARTED)
    assert tool_started.operation_id == operation_id
    preparation = sink.only(EventType.PREPARATION_STARTED)
    preparation_finished = sink.only(EventType.PREPARATION_FINISHED)
    assert preparation.payload["scope"] == "tool_preamble"
    assert preparation.payload["target_operation_id"] == operation_id
    finished = sink.only(EventType.TOOL_OPERATION_FINISHED)
    assert finished.operation_id == operation_id
    assert finished.payload["outcome"] == ToolOutcome.INTERRUPTED
    assert finished.payload["abandoned"] is True
    assert finished.payload["duration_ms"] == 0
    assert tool_started.links[0].target_operation_id == preparation.operation_id
    assert preparation.monotonic_ns <= preparation_finished.monotonic_ns <= tool_started.monotonic_ns
    assert tool_started.monotonic_ns <= finished.monotonic_ns
    sink.assert_operations_settled()


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_record_file_mutations_under_the_tool_operation(kind: str, tmp_path: Path) -> None:
    """Sub-agents and workflow nodes share the sub-agent middleware, so both must forward the id."""
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    middleware = _execution_stamp_middleware(kind, mutation_tracker=tracker, workspace_cwd=str(tmp_path))
    operation_id = new_analytics_id()
    target = tmp_path / "edited.txt"
    context = _ctx(
        "write_file",
        args={"path": str(target), "content": "new\n"},
        metadata={OPERATION_ID_KEY: operation_id},
    )

    async def _next() -> None:
        await asyncio.to_thread(target.write_text, "new\n", encoding="utf-8")
        context.result = "ok"

    await middleware.process(context, _next)

    turn = tracker.current_turn
    assert turn is not None
    assert [mutation.tool_operation_id for mutation in turn.mutations] == [operation_id]
    assert turn.mutations[0].tool_call_id != operation_id


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_parallel_tool_preambles_pair_with_their_own_tool_operations(kind: str) -> None:
    """A concurrent batch keeps every preamble link attached to its own tool."""
    middleware = _execution_stamp_middleware(kind)
    operation_ids = [new_analytics_id() for _ in range(4)]
    entered = 0
    all_entered = asyncio.Event()

    async def invoke(operation_id: str) -> None:
        context = _ctx(f"tool_{operation_id[:4]}", metadata={OPERATION_ID_KEY: operation_id})

        async def _next() -> None:
            nonlocal entered
            entered += 1
            if entered == len(operation_ids):
                all_entered.set()
            await all_entered.wait()
            context.result = "ok"

        await middleware.process(context, _next)

    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        await asyncio.gather(*(invoke(operation_id) for operation_id in operation_ids))

    preambles = {
        str(draft.payload["target_operation_id"]): draft.operation_id
        for draft in sink.of_type(EventType.PREPARATION_STARTED)
        if draft.payload["scope"] == "tool_preamble"
    }
    tool_starts = {draft.operation_id: draft for draft in sink.of_type(EventType.TOOL_OPERATION_STARTED)}
    assert set(preambles) == set(tool_starts) == set(operation_ids)
    for operation_id, tool_start in tool_starts.items():
        caused_by = [link.target_operation_id for link in tool_start.links if link.relation == "caused_by"]
        assert caused_by == [preambles[operation_id]]
    sink.assert_operations_settled()


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_close_an_operation_that_failed_before_its_start_marker(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same window, ordinary exception: the operation still owes a terminal."""
    module = tool_events_module if kind == "main" else sub_agent_events_module

    async def _raises(**_kwargs: object) -> bool:
        raise RuntimeError("hook dispatch blew up")

    monkeypatch.setattr(module, "apply_before_tool_hooks", _raises)
    middleware = _execution_stamp_middleware(kind)
    operation_id = new_analytics_id()
    context = _ctx(
        "write_file",
        "filesystem.write",
        args={"path": "a.txt", "content": "x"},
        metadata={OPERATION_ID_KEY: operation_id},
    )

    async def _next() -> None:
        context.result = "ok"

    sink = FakeSink()
    with trajectory_scope(make_context(sink)), pytest.raises(RuntimeError):
        await middleware.process(context, _next)

    assert sink.only(EventType.TOOL_OPERATION_STARTED).operation_id == operation_id
    finished = sink.only(EventType.TOOL_OPERATION_FINISHED)
    assert finished.payload["outcome"] == ToolOutcome.ERRORED
    assert finished.payload["abandoned"] is True
    sink.assert_operations_settled()


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_close_an_operation_interrupted_in_its_start_marker(kind: str) -> None:
    """The start marker awaits its write ack, and the line lands even when that
    wait is cancelled — so the operation it opened has to be closed here."""
    middleware = _execution_stamp_middleware(kind)
    context = _ctx("write_file", "filesystem.write", args={"path": "a.txt", "content": "x"})

    async def _next() -> None:
        context.result = "ok"

    # Preparation start/finish land first; cancellation hits tool start's ack.
    sink = CancelAckSink(at=3)
    with trajectory_scope(make_context(sink)), pytest.raises(asyncio.CancelledError):
        await middleware.process(context, _next)

    assert sink.only(EventType.TOOL_OPERATION_STARTED)
    assert sink.only(EventType.TOOL_OPERATION_FINISHED).payload["outcome"] == ToolOutcome.INTERRUPTED


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_close_an_operation_interrupted_after_the_call_ran(kind: str) -> None:
    """The tool already ran; an interrupt while closing it out must not leave
    the operation open."""
    middleware = _execution_stamp_middleware(kind)
    context = _ctx("write_file", "filesystem.write", args={"path": "a.txt", "content": "x"})

    async def _next() -> None:
        context.result = "ok"

    # Preparation start/finish precede tool start; 4 is the observed result.
    sink = CancelAckSink(at=4)
    with trajectory_scope(make_context(sink)), pytest.raises(asyncio.CancelledError):
        await middleware.process(context, _next)

    assert sink.only(EventType.TOOL_PAYLOAD_OBSERVED)
    assert sink.only(EventType.TOOL_OPERATION_FINISHED).payload["outcome"] == ToolOutcome.INTERRUPTED


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_record_the_result_the_ceiling_leaves(kind: str) -> None:
    """The kernel bounds the result after this middleware returns — the same
    ceiling a hook's appended context escapes — so a payload measured before
    that describes bytes the model was never handed."""
    middleware = _execution_stamp_middleware(kind, tool_result_ceiling_tokens=100)
    context = _ctx("read_file", "filesystem.read", args={"path": "a.txt"})
    oversized = "word " * 4000

    async def _next() -> None:
        context.result = oversized

    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        await middleware.process(context, _next)

    payload = sink.only(EventType.TOOL_PAYLOAD_OBSERVED).payload
    assert payload["model_visible_bytes"] == len(apply_result_ceiling(oversized, 100).encode())
    assert payload["model_visible_bytes"] < len(oversized.encode())
    assert payload["truncated"] is True
    assert payload["original_bytes"] == len(oversized.encode())


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_record_which_server_or_skill_served_the_call(kind: str) -> None:
    """``search_issues`` and ``load_skill`` say nothing on their own about who answered."""
    middleware = _execution_stamp_middleware(kind)
    mcp_tool = SimpleNamespace(name="search_issues", chrys_kind="mcp")
    set_tool_context(mcp_tool, {"server_name": "github", "remote_name": "search-issues"})
    skill_tool = SimpleNamespace(name="load_skill", chrys_kind="skill")
    set_tool_context_builder(skill_tool, lambda args: {"skill_name": args["skill_name"]})

    async def _next() -> None:
        context.result = "ok"

    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        for function, arguments in ((mcp_tool, {"query": "open"}), (skill_tool, {"skill_name": "pdf-forms"})):
            context = SimpleNamespace(function=function, arguments=arguments, result=None, metadata={})
            await middleware.process(context, _next)

    served_by = [event.payload["tool_context"] for event in sink.of_type(EventType.TOOL_OPERATION_STARTED)]
    assert served_by == [{"server_name": "github", "remote_name": "search-issues"}, {"skill_name": "pdf-forms"}]


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_stamp_structured_error(kind: str) -> None:
    middleware = _execution_stamp_middleware(kind)
    context = _ctx("read_file", "filesystem.read", args={"path": "missing.txt"})

    async def _next() -> None:
        context.result = tool_error("not_found", "missing")

    await middleware.process(context, _next)

    stamp = context.metadata[EXECUTION_STAMP_KEY]
    assert stamp["outcome"] == "error"
    assert stamp["error_kind"] == "not_found"


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_stamp_raised_error(kind: str) -> None:
    middleware = _execution_stamp_middleware(kind)
    context = _ctx("explode", args={"value": 7})

    async def _next() -> None:
        raise LookupError("boom")

    with pytest.raises(LookupError, match="boom"):
        await middleware.process(context, _next)

    stamp = context.metadata[EXECUTION_STAMP_KEY]
    assert stamp["outcome"] == "error"
    assert stamp["error_kind"] == "LookupError"


async def test_execution_stamp_digest_is_stable_and_tracks_hook_rewrite() -> None:
    async def _run(arguments: dict[str, int], hook_manager=None) -> dict[str, str]:
        middleware = ToolEventMiddleware(
            EventBus(),
            session_id="stamp-test",
            hook_manager=hook_manager,
            origin=InvocationOrigin("turn", "stamp-test", "turn-test", None),
        )
        context = _ctx("echo", args=arguments)

        async def _next() -> None:
            context.result = "done"

        await middleware.process(context, _next)
        return context.metadata[EXECUTION_STAMP_KEY]

    first = await _run({"a": 1, "b": 2})
    reordered = await _run({"b": 2, "a": 1})
    rewrite = RewriteArgsHookManager({HookEvent.BEFORE_TOOL_CALL: HookDecision(args_override={"a": 9})})
    rewritten = await _run({"a": 1, "b": 2}, rewrite)

    assert first["effective_args_digest"] == reordered["effective_args_digest"]
    assert rewritten["effective_args"] == '{"a":9,"b":2}'
    assert rewritten["effective_args_digest"] != first["effective_args_digest"]


def test_execution_stamp_caps_effective_arguments_by_middle_truncation() -> None:
    stamp = build_execution_stamp({"payload": "x" * 4_096}, outcome="ok")

    assert len(stamp["effective_args"]) == EFFECTIVE_ARGS_MAX_CHARS
    assert "…" in stamp["effective_args"]


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_stamp_typed_mapping_keys_without_failing_tool(kind: str) -> None:
    middleware = _execution_stamp_middleware(kind)
    context = _ctx("dated", args={"values": {date(2026, 7, 18): "ok"}})

    async def _next() -> None:
        context.result = "done"

    await middleware.process(context, _next)

    assert context.result == "done"
    stamp = context.metadata[EXECUTION_STAMP_KEY]
    assert stamp["effective_args"] == '{"values":{"2026-07-18":"ok"}}'
    assert stamp["outcome"] == "ok"


@pytest.mark.parametrize("kind", ["main", "sub_agent"])
async def test_event_middlewares_never_fail_tool_when_stamp_serialization_fails(kind: str) -> None:
    class _Unserializable:
        def __str__(self) -> str:
            raise RuntimeError("cannot stringify")

    middleware = _execution_stamp_middleware(kind)
    context = _ctx("opaque", args={"value": _Unserializable()})

    async def _next() -> None:
        context.result = "done"

    await middleware.process(context, _next)

    assert context.result == "done"
    assert EXECUTION_STAMP_KEY not in context.metadata
