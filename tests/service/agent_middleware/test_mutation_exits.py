# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Main and child tool exits preserve file facts without phantom deletions."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationToolCallStart, ShellToBackground
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE, KIND_SHELL
from chrys.service.agent_middleware import SubAgentEventMiddleware, ToolEventMiddleware
from chrys.service.agent_middleware._metadata_keys import _APPROVAL_REJECTED_KEY
from chrys.service.agent_middleware.control.hook_commit import HookCommitMiddleware
from chrys.service.agent_middleware.events.hosted_tools import HostedPresentationBridge
from chrys.service.mutations.coordination import MutationCoordinator
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from tests.service.agent_middleware._event_fakes import _ctx
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


@pytest.mark.parametrize("child", [False, True], ids=["main", "child"])
@pytest.mark.parametrize(
    "exit_at", ["rejected", "call-cancelled", "start-cancelled", "start-error", "callback-cancelled", "callback-error"]
)
async def test_file_exit_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: bool, exit_at: str) -> None:
    target = tmp_path / "file.txt"
    await asyncio.to_thread(target.write_text, "original", encoding="utf-8")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    bus = EventBus()
    error = RuntimeError("start failed") if exit_at.endswith("error") else asyncio.CancelledError("interrupted")
    if child:
        middleware = SubAgentEventMiddleware(
            bus,
            "Child",
            "child",
            mutation_tracker=tracker,
            origin=InvocationOrigin("sub_agent", "session", "child", None),
        )
        bridge = create_autospec(HostedPresentationBridge, instance=True)
        middleware._hosted_bridge = bridge
    else:
        middleware = ToolEventMiddleware(
            bus, mutation_tracker=tracker, origin=InvocationOrigin("turn", "session", "turn", None)
        )
    context = _ctx("edit_file", KIND_FILESYSTEM_WRITE, args={"path": str(target)})
    calls: list[str] = []

    async def start_callback(call_id: str) -> None:
        if exit_at.startswith("callback"):
            raise error

    if child:
        bridge.local_call_start_published.side_effect = start_callback
    else:
        middleware._on_start_published = start_callback
    original_publish = bus.publish

    async def publish(event):
        if isinstance(event, InvocationToolCallStart) and exit_at.startswith("start"):
            raise error
        await original_publish(event)

    monkeypatch.setattr(bus, "publish", publish)

    async def call() -> None:
        calls.append("executed")
        await asyncio.to_thread(target.write_text, "partial write", encoding="utf-8")
        raise error

    async def approval_or_call() -> None:
        if exit_at == "rejected":
            context.metadata[_APPROVAL_REJECTED_KEY] = True
            context.result = "Error: rejected"
            return
        await HookCommitMiddleware().process(context, call)

    if exit_at == "rejected":
        await middleware.process(context, approval_or_call)
    else:
        with pytest.raises(type(error)) as raised:
            await middleware.process(context, approval_or_call)
        assert raised.value is error

    (row,) = tracker.get_turn_mutations(1).mutations
    assert tracker.store.read_blob(row.before_hash) == b"original"
    assert tracker.store.read_blob(row.after_hash) == (b"partial write" if exit_at == "call-cancelled" else b"original")
    assert calls == (["executed"] if exit_at == "call-cancelled" else [])
    assert tracker.get_rollback_plan(1).paths == ({str(target)} if calls else set())


def _file_middleware(bus: EventBus, tracker: MutationTracker, *, child: bool):
    if not child:
        return ToolEventMiddleware(
            bus, mutation_tracker=tracker, origin=InvocationOrigin("turn", "session", "turn", None)
        )
    middleware = SubAgentEventMiddleware(
        bus, "Child", "child", mutation_tracker=tracker, origin=InvocationOrigin("sub_agent", "session", "child", None)
    )
    middleware._hosted_bridge = create_autospec(HostedPresentationBridge, instance=True)
    return middleware


@pytest.mark.parametrize("child", [False, True], ids=["main", "child"])
@pytest.mark.parametrize("committed", [True, False], ids=["writer-running", "awaiting-approval"])
async def test_interrupted_edit_proves_no_change_only_when_it_cannot_still_write(
    tmp_path: Path, child: bool, committed: bool
) -> None:
    target = tmp_path / "file.txt"
    await asyncio.to_thread(target.write_text, "original", encoding="utf-8")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    middleware = _file_middleware(EventBus(), tracker, child=child)
    context = _ctx("edit_file", KIND_FILESYSTEM_WRITE, args={"path": str(target)})
    writing, release, written = threading.Event(), threading.Event(), threading.Event()
    awaiting_approval = asyncio.Event()

    def write() -> None:
        writing.set()
        if release.wait(ENGINE_TURN_TIMEOUT):
            target.write_text("edited", encoding="utf-8")
        written.set()

    async def tool() -> None:
        # As FunctionTool runs a sync tool: a cancel abandons the await, never the thread.
        await asyncio.get_running_loop().run_in_executor(None, write)

    async def approval_then_commit() -> None:
        if not committed:
            awaiting_approval.set()
            await asyncio.Event().wait()
        await HookCommitMiddleware().process(context, tool)

    task = asyncio.create_task(middleware.process(context, approval_then_commit))
    try:
        ready = writing.is_set if committed else awaiting_approval.is_set
        await wait_for(lambda: ready() or task.done(), description="call in flight")
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    if committed:
        await wait_for(written.is_set, description="abandoned writer finished")

    (row,) = tracker.get_turn_mutations(1).mutations
    # The endpoint was read at the interrupt, before the abandoned writer landed.
    assert tracker.store.read_blob(row.after_hash) == b"original"
    assert row.proves_no_change is not committed
    assert tracker.get_rollback_plan(1).paths == ({str(target)} if committed else set())
    assert await asyncio.to_thread(target.read_text, encoding="utf-8") == ("edited" if committed else "original")


@pytest.mark.parametrize("exit_at", ["main-start", "child-start", "main-callback", "child-callback", "shell-subscribe"])
@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_pre_execution_shell_exits_close_the_window_without_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_at: str, failure: type[BaseException]
) -> None:
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    bus = EventBus()
    coordinator = create_autospec(MutationCoordinator, instance=True)
    coordinator.open_window.return_value = "window"
    error = failure("before execution")
    child = exit_at.startswith("child")
    if child:
        middleware = SubAgentEventMiddleware(
            bus,
            "Child",
            "child",
            mutation_tracker=tracker,
            mutation_coordinator=coordinator,
            workspace_cwd=str(tmp_path),
            origin=InvocationOrigin("sub_agent", "session", "child", None),
        )
        bridge = create_autospec(HostedPresentationBridge, instance=True)
        middleware._hosted_bridge = bridge
    else:
        middleware = ToolEventMiddleware(
            bus,
            mutation_tracker=tracker,
            mutation_coordinator=coordinator,
            workspace_cwd=str(tmp_path),
            origin=InvocationOrigin("turn", "session", "turn", None),
        )

    async def callback(call_id: str) -> None:
        if exit_at.endswith("callback"):
            raise error

    if child:
        bridge.local_call_start_published.side_effect = callback
    else:
        middleware._on_start_published = callback
    original_publish, original_subscribe = bus.publish, bus.subscribe

    async def publish(event):
        if isinstance(event, InvocationToolCallStart) and exit_at.endswith("start"):
            raise error
        await original_publish(event)

    async def subscribe(event_type, handler):
        if event_type is ShellToBackground:
            raise error
        await original_subscribe(event_type, handler)

    monkeypatch.setattr(bus, "publish", publish)
    if exit_at == "shell-subscribe":
        monkeypatch.setattr(bus, "subscribe", subscribe)
    calls: list[str] = []

    async def execute() -> None:
        calls.append("executed")

    context = _ctx("shell", KIND_SHELL, args={"command": "true", "workdir": str(tmp_path)})
    with pytest.raises(failure) as raised:
        await middleware.process(context, execute)

    assert raised.value is error
    assert calls == []
    coordinator.close_window.assert_called_once_with("window")
    assert tracker.get_turn_mutations(1).detection_truncated is False
