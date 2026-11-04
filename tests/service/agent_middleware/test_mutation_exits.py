# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Main and child tool exits preserve file facts without phantom deletions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationToolCallStart, ShellToBackground
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE, KIND_SHELL
from chrys.service.agent_middleware import SubAgentEventMiddleware, ToolEventMiddleware
from chrys.service.agent_middleware._metadata_keys import _APPROVAL_REJECTED_KEY
from chrys.service.agent_middleware.events.hosted_tools import HostedPresentationBridge
from chrys.service.mutations.coordination import MutationCoordinator
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from tests.service.agent_middleware._event_fakes import _ctx


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

    async def approval_or_call() -> None:
        if exit_at == "rejected":
            context.metadata[_APPROVAL_REJECTED_KEY] = True
            context.result = "Error: rejected"
            return
        calls.append("executed")
        await asyncio.to_thread(target.write_text, "partial write", encoding="utf-8")
        raise error

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
