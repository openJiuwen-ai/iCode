# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reusable approval boundaries for edited, typed and concurrently stored requests."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, create_autospec

import pytest
from pydantic import create_model

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.util.lock import FileLock
from chrys.kernel import FunctionTool
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.policy import ApprovalPolicy
from chrys.service.approval.reuse_binding import ApprovalReuseBinding
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.schema import HookDecision
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.tools.builtins.shell import ShellTools
from tests.kernel._fakes import _call_response, _result_contents, _stack, _text_response, _user
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT


@pytest.fixture
def approval_setup(tmp_path):
    runtime = SessionEnvironment.capture("session-a", Workspace.from_cwd(str(tmp_path)))
    runtime = replace(
        runtime,
        platform=replace(
            runtime.platform, config_dir=tmp_path / "config", shell=replace(runtime.platform.shell, name="bash")
        ),
    )
    tools = ShellTools(runtime).tools()
    return (
        tools[0],
        ApprovalReuseBinding(runtime, tools),
        EventBus(),
        ApprovalPolicy(ApprovalConfig(default="require")),
    )


@pytest.mark.parametrize("rewrite", [False, True])
async def test_ui_edit_runs_its_before_hook_once(approval_setup, rewrite):
    tool, binding, bus, policy = approval_setup
    hooks = create_autospec(HookManager, instance=True)
    hooks.has_hooks_for.side_effect = lambda event: event == HookEvent.BEFORE_TOOL_CALL
    seen = []

    async def hook(
        event, payload, *, scope="turn", target_operation_id=None, trajectory_context=None, origin=None, operation=None
    ):
        assert scope == "turn"
        command = payload["tool"]["args"]["command"]
        seen.append(command)
        return HookDecision(args_override={"command": f"timeout 10 {command}"}) if rewrite else HookDecision()

    hooks.fire.side_effect = hook
    requests = []

    async def approve(event):
        requests.append(event.args["command"])
        await bus.publish(
            ApprovalResponse(
                request_id=event.request_id,
                approved=True,
                modified_args={"command": "npm run build"} if len(requests) == 1 else None,
            )
        )

    middleware = ApprovalMiddleware(policy, bus, reuse=binding, hook_manager=hooks)
    context = FunctionInvocationContext(tool, {"command": "npm run test"})
    called = AsyncMock(spec=lambda: None)
    await bus.subscribe(ApprovalRequest, approve)
    try:
        await middleware.process(context, called)
        final = "timeout 10 npm run build" if rewrite else "npm run build"
        assert seen == ["npm run build"]
        assert requests == ["npm run test"]
        assert context.arguments == {"command": final}
        called.assert_awaited_once()
        assert binding.service.rules() == []
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


def reuse_candidate(binding, context):
    return binding.prepare(context, reusable=True).candidate


def _hold_writer_lock(store):
    lock = FileLock(store.lock_path, timeout=1)
    lock.acquire()
    return lock


def _record_calls(monkeypatch, owner, name, threads, gate=None):
    """Record the thread of each ``owner.<name>`` call; off the loop, wait for *gate* first.

    A worker held at the gate cannot finish before the loop runs the callback
    that opens it, so the ``to_thread`` awaiting it must yield to the loop.
    (A worker that finishes before its future is awaited returns without one.)
    """
    real = getattr(owner, name)
    loop_thread = threading.get_ident()

    def call(*args):
        threads.append(threading.get_ident())
        if gate is not None and threads[-1] != loop_thread:
            gate.wait(DEFAULT_WAIT_TIMEOUT)
        return real(*args)

    monkeypatch.setattr(owner, name, call)


@pytest.mark.parametrize("operation", ["match", "remember"])
async def test_writer_contention_does_not_block_approval_event_loop(approval_setup, monkeypatch, operation):
    tool, binding, bus, policy = approval_setup
    context = FunctionInvocationContext(tool, {"command": "npm run test"})
    candidate = reuse_candidate(binding, context)
    assert candidate is not None
    if operation == "match":
        assert await asyncio.to_thread(binding.service.remember, candidate, "EXACT_PROJECT")
    else:
        assert await asyncio.to_thread(binding.service.store.add_many, [])
    lock = await asyncio.to_thread(_hold_writer_lock, binding.service.store)
    heartbeat = threading.Event()
    read_threads = []
    write_threads = []
    for store in (binding.service.store, binding.service.session_store):
        _record_calls(monkeypatch, store, "load", read_threads, heartbeat if operation == "match" else None)
    _record_calls(monkeypatch, binding.service.store, "add_many", write_threads, heartbeat)
    order = []
    requests = []

    def beat():
        order.append("heartbeat")
        # A grant read never waits for the writer lock, so the match case keeps it held.
        if operation == "remember":
            lock.release()
        heartbeat.set()

    async def approve(event):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True, remember_choice="EXACT_PROJECT"))
        if operation == "remember":
            asyncio.get_running_loop().call_soon(beat)

    async def execute():
        order.append("execute")

    middleware = ApprovalMiddleware(policy, bus, reuse=binding)
    await bus.subscribe(ApprovalRequest, approve)
    try:
        if operation == "match":
            asyncio.get_running_loop().call_soon(beat)
        await middleware.process(context, execute)
        assert order == ["heartbeat", "execute"]
        assert read_threads
        assert threading.get_ident() not in read_threads + write_threads
        assert len(write_threads) == (0 if operation == "match" else 1)
        assert len(requests) == (0 if operation == "match" else 1)
        assert await asyncio.to_thread(binding.service.match, candidate)
    finally:
        # Free a gated worker and run a queued heartbeat when a regression failed above.
        heartbeat.set()
        await asyncio.sleep(0)
        lock.release()
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


async def test_reserved_writer_lock_does_not_block_existing_rule_reads(approval_setup):
    tool, binding, _, _ = approval_setup
    candidate = reuse_candidate(binding, FunctionInvocationContext(tool, {"command": "npm run test"}))
    assert candidate is not None
    assert await asyncio.to_thread(binding.service.remember, candidate, "EXACT_PROJECT")
    lock = await asyncio.to_thread(_hold_writer_lock, binding.service.store)
    try:
        assert await asyncio.to_thread(binding.service.match, candidate)
    finally:
        lock.release()


async def test_session_grants_survive_rebuild_but_not_a_different_session(approval_setup):
    tool, binding, bus, policy = approval_setup
    context = FunctionInvocationContext(tool, {"command": "npm run test"})
    candidate = reuse_candidate(binding, context)
    assert candidate is not None
    assert await asyncio.to_thread(binding.service.remember, candidate, "EXACT_SESSION")
    middleware = ApprovalMiddleware(policy, bus, reuse=binding)
    await middleware.close()
    # An empty session id means the runtime's own session.
    rebuilt = ApprovalReuseBinding(binding.runtime, [tool], session_id="")
    other_session = ApprovalReuseBinding(binding.runtime, [tool], session_id="session-b")
    assert await asyncio.to_thread(rebuilt.service.match, reuse_candidate(rebuilt, context))
    assert not await asyncio.to_thread(other_session.service.match, reuse_candidate(other_session, context))
    assert await asyncio.to_thread(other_session.service.rules) == []


@pytest.mark.parametrize(
    ("annotation", "wire_value", "expected_type"),
    [(tuple[int, ...], [1, 2], tuple), (set[int], [1, 2], set), (Path, "relative", Path), (float, float("nan"), float)],
)
async def test_typed_values_reach_ordinary_approval_through_real_tool_loop(
    approval_setup, annotation, wire_value, expected_type
):
    _, binding, bus, policy = approval_setup
    executed = []
    requests = []

    async def consume(value: Any) -> str:
        executed.append(value)
        return "typed tool completed"

    tool = FunctionTool(
        name="typed_value", func=consume, input_model=create_model("TypedValue", value=(annotation, ...))
    )
    middleware = ApprovalMiddleware(policy, bus, reuse=binding)

    async def approve(event):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True))

    await bus.subscribe(ApprovalRequest, approve)
    try:
        layer, _ = _stack(
            [_call_response(("call-typed", "typed_value", {"value": wire_value})), _text_response()],
            middleware=middleware,
        )
        response = await layer.get_response([_user()], options={"tools": [tool]})
        assert len(requests) == len(executed) == 1
        assert isinstance(requests[0].args["value"], expected_type)
        assert isinstance(executed[0], expected_type)
        assert _result_contents(response)[0].result == "typed tool completed"
        assert binding.service.rules() == []
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)
