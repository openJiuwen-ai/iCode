# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ApprovalMiddleware`` retracts a request whose wait ends unanswered.

A frontend keeps an approval request (an open dialog, or one held unseen while
the judge reviews it) until the backend says it is gone. Every wait that ends
without an answer — interrupt, failure, cancel during publication — publishes
exactly one ``ApprovalCancelled``, after any verdict; an answered request never
does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalCancelled,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalReviewed,
    Event,
)
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType
from chrys.kernel import ChatResponse, Message
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval import judge as judge_module
from chrys.service.approval.judge import JudgeVerdict
from chrys.service.approval.policy import ApprovalMode
from chrys.service.hooks.manager import HookManager
from tests.service.agent_middleware.test_approval import _ctx, _require_all_policy
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.formal_approval import judge_client, predicate_values
from tests.support.waiting import wait_for

_SESSION_ID = "sess-1"


class _HeldJudge:
    """A judge that answers only when the test releases it (never, unless it does)."""

    def __init__(self, verdict: JudgeVerdict | None = None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self._verdict = verdict

    async def evaluate(
        self,
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
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self._verdict is None:
            raise AssertionError("released a judge that has no verdict")
        return self._verdict


class _Recorder:
    """Records approval traffic on one bus, in publication order."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    @classmethod
    async def attach(cls, bus: EventBus) -> _Recorder:
        recorder = cls()
        for event_type in (ApprovalRequest, ApprovalReviewed, ApprovalCancelled):
            await bus.subscribe(event_type, recorder._record)
        return recorder

    async def _record(self, event: Event) -> None:
        self.events.append(event)

    def of[E: Event](self, event_type: type[E]) -> list[E]:
        return [event for event in self.events if isinstance(event, event_type)]

    def request(self) -> ApprovalRequest:
        (request,) = self.of(ApprovalRequest)
        return request


def _middleware(
    bus: EventBus, *, judge: _HeldJudge | None = None, hooks: HookManager | None = None
) -> ApprovalMiddleware:
    return ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO if judge is not None else ApprovalMode.MANUAL,
        approval_judge=judge,
        session_id=_SESSION_ID,
        hook_manager=hooks,
    )


def _failing_hooks() -> HookManager:
    """A hook manager whose approval-requested notification fails, ending the wait with an error."""
    hooks = create_autospec(HookManager, instance=True)
    hooks.has_hooks_for.return_value = True
    hooks.fire.side_effect = RuntimeError("hook manager failed")
    return hooks


def _write_call() -> MagicMock:
    return _ctx("write_file", "filesystem.write", {"path": "/tmp/x"})


async def _tool_must_not_run() -> None:
    raise AssertionError("the tool must not run")


@contextlib.asynccontextmanager
async def _owned(
    mw: ApprovalMiddleware,
    *,
    tool: Callable[[], Awaitable[None]] = _tool_must_not_run,
    gates: tuple[asyncio.Event, ...] = (),
) -> AsyncIterator[asyncio.Task[None]]:
    """Run one call; on exit open *gates*, then cancel and reap the call and close *mw*."""
    task = asyncio.create_task(mw.process(_write_call(), tool))
    try:
        yield task
    finally:
        for gate in gates:
            gate.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await mw.close()


async def _interrupt(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("stage", ["judge_reviewing", "judge_flagged", "manual"])
async def test_an_interrupted_wait_retracts_its_request_once(stage: str) -> None:
    bus = EventBus()
    recorder = await _Recorder.attach(bus)
    judge = None
    if stage != "manual":
        judge = _HeldJudge(JudgeVerdict(approved=False, reason="flagged"))
    gates = (judge.release,) if judge is not None else ()
    async with _owned(_middleware(bus, judge=judge), gates=gates) as task:
        await wait_for(lambda: bool(recorder.of(ApprovalRequest)) or task.done(), description="approval request")
        assert not task.done()
        if judge is not None:
            await wait_for(lambda: judge.entered.is_set() or task.done(), description="judge reached")
            if stage == "judge_flagged":
                judge.release.set()
                await wait_for(lambda: bool(recorder.of(ApprovalReviewed)) or task.done(), description="judge verdict")
            assert not task.done()
        await _interrupt(task)

    cancels = recorder.of(ApprovalCancelled)
    assert [(event.request_id, event.session_id) for event in cancels] == [(recorder.request().request_id, _SESSION_ID)]
    # The retraction is the last word on the request: no verdict follows it.
    assert recorder.events[-1] is cancels[0]


async def _answer_by_user(bus: EventBus, recorder: _Recorder, task: asyncio.Task[None], *, approved: bool) -> None:
    await wait_for(lambda: bool(recorder.of(ApprovalRequest)) or task.done(), description="approval request")
    assert not task.done()
    await bus.publish(ApprovalResponse(request_id=recorder.request().request_id, approved=approved))


@pytest.mark.parametrize("answer", ["user_approves", "user_rejects", "judge_approves"])
async def test_an_answered_request_is_never_retracted(answer: str) -> None:
    bus = EventBus()
    recorder = await _Recorder.attach(bus)
    judge = _HeldJudge(JudgeVerdict(approved=True, reason="safe")) if answer == "judge_approves" else None
    ran: list[str] = []

    async def _tool() -> None:
        ran.append("tool")

    gates = (judge.release,) if judge is not None else ()
    async with _owned(_middleware(bus, judge=judge), tool=_tool, gates=gates) as task:
        if judge is not None:
            await wait_for(lambda: judge.entered.is_set() or task.done(), description="judge reached")
            judge.release.set()
        else:
            await _answer_by_user(bus, recorder, task, approved=answer == "user_approves")
        await task

    assert ran == ([] if answer == "user_rejects" else ["tool"])
    assert recorder.of(ApprovalCancelled) == []


class _InterruptedResolutionSink(FakeSink):
    """A sink whose resolution marker is interrupted, as an Esc on its write ack would be."""

    async def emit(self, draft, *, payload_factory=None):  # type: ignore[no-untyped-def]
        if draft.event_type == EventType.APPROVAL_RESOLVED:
            raise asyncio.CancelledError
        return await super().emit(draft, payload_factory=payload_factory)


async def test_an_interrupt_after_the_answer_does_not_retract() -> None:
    """The frontend already closed an answered request; a retraction would contradict it."""
    bus = EventBus()
    recorder = await _Recorder.attach(bus)

    async def _answer(event: ApprovalRequest) -> None:
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True))

    await bus.subscribe(ApprovalRequest, _answer)
    mw = _middleware(bus)
    try:
        with trajectory_scope(make_context(_InterruptedResolutionSink())), pytest.raises(asyncio.CancelledError):
            await mw.process(_write_call(), _tool_must_not_run)
    finally:
        await mw.close()

    assert len(recorder.of(ApprovalRequest)) == 1
    assert recorder.of(ApprovalCancelled) == []


async def test_the_retraction_survives_a_second_cancel() -> None:
    """A second interrupt during cleanup must not cut the retraction short."""
    bus = EventBus()
    recorder = await _Recorder.attach(bus)
    retracting = asyncio.Event()
    release = asyncio.Event()
    delivered: list[str] = []

    async def _slow_frontend(event: ApprovalCancelled) -> None:
        retracting.set()
        await release.wait()
        delivered.append(event.request_id)

    await bus.subscribe(ApprovalCancelled, _slow_frontend)
    async with _owned(_middleware(bus), gates=(release,)) as task:
        await wait_for(lambda: bool(recorder.of(ApprovalRequest)) or task.done(), description="approval request")
        assert not task.done()

        task.cancel()
        await wait_for(lambda: retracting.is_set() or task.done(), description="retraction in flight")
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)  # deliver the second cancel before the frontend finishes
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert delivered == [recorder.request().request_id]


async def test_a_cancel_during_request_publication_retracts_the_request() -> None:
    """Handlers that already ran may hold the request although publish never returned."""
    bus = EventBus()
    recorder = await _Recorder.attach(bus)
    publishing = asyncio.Event()
    unstuck = asyncio.Event()

    async def _stuck_frontend(_event: ApprovalRequest) -> None:
        publishing.set()
        await unstuck.wait()

    await bus.subscribe(ApprovalRequest, _stuck_frontend)
    async with _owned(_middleware(bus), gates=(unstuck,)) as task:
        await wait_for(lambda: publishing.is_set() or task.done(), description="request publication")
        assert not task.done()
        await _interrupt(task)

    assert [event.request_id for event in recorder.of(ApprovalCancelled)] == [recorder.request().request_id]


async def test_a_verdict_never_follows_the_retraction() -> None:
    """The judge is drained before the retraction goes out, so no verdict can trail it."""
    bus = EventBus()
    recorder = await _Recorder.attach(bus)
    judge = _HeldJudge(JudgeVerdict(approved=False, reason="flagged"))

    async def _frontend(_event: ApprovalCancelled) -> None:
        # A judge still running here could publish its verdict now: free it
        # and wait until it is either gone or has spoken.
        judge.release.set()
        await wait_for(lambda: judge.cancelled or bool(recorder.of(ApprovalReviewed)), description="judge settled")

    await bus.subscribe(ApprovalCancelled, _frontend)
    async with _owned(_middleware(bus, judge=judge), gates=(judge.release,)) as task:
        await wait_for(lambda: judge.entered.is_set() or task.done(), description="judge reached")
        assert not task.done()
        await _interrupt(task)

    assert recorder.of(ApprovalReviewed) == []
    assert recorder.events[-1] is recorder.of(ApprovalCancelled)[0]


async def test_a_failed_wait_retracts_its_request() -> None:
    bus = EventBus()
    recorder = await _Recorder.attach(bus)
    mw = _middleware(bus, hooks=_failing_hooks())
    try:
        with pytest.raises(RuntimeError, match="hook manager failed"):
            await mw.process(_write_call(), _tool_must_not_run)
    finally:
        await mw.close()

    assert [event.request_id for event in recorder.of(ApprovalCancelled)] == [recorder.request().request_id]


async def test_an_interrupt_during_a_failed_wait_retraction_is_not_lost() -> None:
    """A cancel that lands while a failed wait retracts its request still ends the call as cancelled."""
    bus = EventBus()
    recorder = await _Recorder.attach(bus)
    retracting = asyncio.Event()
    release = asyncio.Event()

    async def _slow_frontend(_event: ApprovalCancelled) -> None:
        retracting.set()
        await release.wait()

    await bus.subscribe(ApprovalCancelled, _slow_frontend)
    async with _owned(_middleware(bus, hooks=_failing_hooks()), gates=(release,)) as task:
        await wait_for(lambda: retracting.is_set() or task.done(), description="retraction in flight")
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)  # deliver the cancel before the frontend finishes
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(recorder.of(ApprovalCancelled)) == 1


@pytest.mark.parametrize("cancel_during_write", [False, True])
async def test_formal_cancellation_on_either_side_of_request_write_ack(monkeypatch, cancel_during_write):
    writing, written, reviewing, release = (asyncio.Event() for _ in range(4))

    class HeldWriteAckSink(FakeSink):
        async def emit(self, draft, *, payload_factory=None):
            result = await super().emit(draft, payload_factory=payload_factory)
            if draft.event_type == EventType.APPROVAL_REQUESTED:
                writing.set()
                await written.wait()
            return result

    async def response(client, messages, *, stream, options, timeout):
        reviewing.set()
        await release.wait()
        return ChatResponse(messages=[Message("assistant", [json.dumps(predicate_values())])])

    respond = create_autospec(judge_module.get_final_response, side_effect=response)
    monkeypatch.setattr(judge_module, "get_final_response", respond)
    bus, sink = EventBus(), HeldWriteAckSink()
    recorder = await _Recorder.attach(bus)
    async with judge_client(monkeypatch, []) as (judge, _):
        mw = _middleware(bus, judge=judge)
        mw.set_user_message("Inspect the file")
        with trajectory_scope(make_context(sink)):
            async with _owned(mw, gates=(written, release)) as task:
                await wait_for(writing.is_set, description="approval request write acknowledgement held")
                respond.assert_not_awaited()
                assert not task.done()
                if not cancel_during_write:
                    written.set()
                    await wait_for(reviewing.is_set, description="Formal model review started")
                await _interrupt(task)
        resolved = sink.only(EventType.APPROVAL_RESOLVED).payload
        assert resolved["decision"] == "interrupted"
        assert resolved["decider"] == "none"
        if cancel_during_write:
            respond.assert_not_awaited()
            assert "formal_judge" not in resolved
        else:
            respond.assert_awaited_once()
            assert resolved["formal_judge"]["failure_reason"] == "cancelled"
        assert recorder.of(ApprovalReviewed) == []
        assert len(recorder.of(ApprovalCancelled)) == 1


@pytest.mark.parametrize("interrupt", [False, True])
async def test_formal_terminal_keeps_audit_when_human_or_interrupt_wins(monkeypatch, interrupt):
    bus, sink = EventBus(), FakeSink()
    recorder = await _Recorder.attach(bus)
    entered, release = asyncio.Event(), asyncio.Event()

    async def response(client, messages, *, stream, options, timeout):
        entered.set()
        if interrupt:
            await release.wait()
        else:
            await bus.publish(ApprovalResponse(request_id=recorder.request().request_id, approved=False))
        return ChatResponse(messages=[Message("assistant", [json.dumps(predicate_values())])])

    monkeypatch.setattr(
        judge_module, "get_final_response", create_autospec(judge_module.get_final_response, side_effect=response)
    )
    async with judge_client(monkeypatch, []) as (judge, _):
        mw = _middleware(bus, judge=judge)
        mw.set_user_message("Inspect the file")
        with trajectory_scope(make_context(sink)):
            async with _owned(mw, gates=(release,)) as task:
                await wait_for(lambda: entered.is_set() or task.done(), description="Formal request started")
                if interrupt:
                    assert not task.done()
                    await _interrupt(task)
                else:
                    await task
        resolved = sink.only(EventType.APPROVAL_RESOLVED).payload
        assert resolved["decision"] == ("interrupted" if interrupt else "rejected")
        assert resolved["decider"] == ("none" if interrupt else "user")
        assert resolved["formal_judge"].get("failure_reason") == ("cancelled" if interrupt else None)
        assert resolved["formal_judge"]["approved"] is (not interrupt)
        assert recorder.of(ApprovalReviewed) == []
