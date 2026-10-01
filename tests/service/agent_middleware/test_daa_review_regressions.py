# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""DAA approval boundaries for edited, typed and concurrently stored requests."""

from __future__ import annotations

import asyncio
import sqlite3
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
from chrys.kernel import FunctionTool
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.daa_binding import DAABinding
from chrys.service.approval.policy import ApprovalPolicy
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.schema import HookDecision
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.tools.builtins.shell import ShellTools
from tests.kernel._fakes import _call_response, _result_contents, _stack, _text_response, _user


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
        DAABinding(runtime, tools, "profile"),
        EventBus(),
        ApprovalPolicy(ApprovalConfig(default="require")),
    )


@pytest.mark.parametrize("rewrite", [False, True])
async def test_ui_edit_runs_its_before_hook_once(approval_setup, rewrite):
    tool, binding, bus, policy = approval_setup
    hooks = create_autospec(HookManager, instance=True)
    hooks.has_hooks_for.side_effect = lambda event: event == HookEvent.BEFORE_TOOL_CALL
    seen = []

    async def hook(event, payload, *, target_operation_id=None):
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

    middleware = ApprovalMiddleware(policy, bus, daa=binding, hook_manager=hooks)
    context = FunctionInvocationContext(tool, {"command": "npm run test"})
    called = AsyncMock(spec=lambda: None)
    await bus.subscribe(ApprovalRequest, approve)
    try:
        await middleware.process(context, called)
        final = "timeout 10 npm run build" if rewrite else "npm run build"
        assert seen == ["npm run build"]
        assert requests == (["npm run test", final] if rewrite else ["npm run test"])
        assert context.arguments == {"command": final}
        called.assert_awaited_once()
        assert binding.service.rules() == []
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), (1, 2), {1, 2}, Path("relative")])
@pytest.mark.parametrize("phase", ["initial", "changed", "edited"])
async def test_non_json_arguments_fall_back_to_ordinary_approval(approval_setup, value, phase):
    _, binding, bus, policy = approval_setup
    tool = FunctionTool(name="custom_tool")
    context = FunctionInvocationContext(tool, {"value": value if phase == "initial" else "original"})
    requests = []

    async def approve(event):
        requests.append(event)
        if phase == "changed" and len(requests) == 1:
            context.arguments["value"] = value
        await bus.publish(
            ApprovalResponse(
                request_id=event.request_id,
                approved=True,
                modified_args={"value": value} if phase == "edited" else None,
                daa_choice="EXACT_PROJECT",
            )
        )

    called = AsyncMock(spec=lambda: None)
    middleware = ApprovalMiddleware(policy, bus, daa=binding)
    await bus.subscribe(ApprovalRequest, approve)
    try:
        await middleware.process(context, called)
        assert len(requests) == (2 if phase == "changed" else 1)
        assert all(not request.daa_exact and not request.daa_prefix for request in requests)
        called.assert_awaited_once()
        assert binding.service.rules() == []
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


def _hold_database(path, mode):
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.execute(f"BEGIN {mode}")
    return connection


@pytest.mark.parametrize("operation", ["match", "remember"])
async def test_sqlite_contention_does_not_block_approval_event_loop(approval_setup, operation):
    tool, binding, bus, policy = approval_setup
    context = FunctionInvocationContext(tool, {"command": "npm run test"})
    candidate = binding.candidate(context)
    assert candidate is not None
    if operation == "match":
        assert await asyncio.to_thread(binding.service.remember, candidate, "EXACT_PROJECT")
    else:
        assert await asyncio.to_thread(binding.service.store.add_many, [])
    connection = await asyncio.to_thread(
        _hold_database, binding.service.store.path, "EXCLUSIVE" if operation == "match" else "IMMEDIATE"
    )
    order = []
    requests = []

    def release():
        order.append("heartbeat")
        connection.rollback()

    async def approve(event):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True, daa_choice="EXACT_PROJECT"))
        if operation == "remember":
            asyncio.get_running_loop().call_soon(release)

    async def execute():
        order.append("execute")

    middleware = ApprovalMiddleware(policy, bus, daa=binding)
    await bus.subscribe(ApprovalRequest, approve)
    try:
        if operation == "match":
            asyncio.get_running_loop().call_soon(release)
        await middleware.process(context, execute)
        assert order == ["heartbeat", "execute"]
        assert len(requests) == (0 if operation == "match" else 1)
        assert await asyncio.to_thread(binding.service.match, candidate) == "HIT_ALLOW"
    finally:
        # Drain a queued release even when a synchronous regression failed above.
        await asyncio.to_thread(lambda: None)
        connection.rollback()
        connection.close()
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


async def test_reserved_writer_lock_does_not_block_existing_rule_reads(approval_setup):
    tool, binding, _, _ = approval_setup
    candidate = binding.candidate(FunctionInvocationContext(tool, {"command": "npm run test"}))
    assert candidate is not None
    assert await asyncio.to_thread(binding.service.remember, candidate, "EXACT_PROJECT")
    connection = await asyncio.to_thread(_hold_database, binding.service.store.path, "IMMEDIATE")
    try:
        assert await asyncio.to_thread(binding.service.match, candidate) == "HIT_ALLOW"
    finally:
        connection.rollback()
        connection.close()


async def test_session_grants_survive_rebuild_but_not_a_different_session(approval_setup):
    tool, binding, bus, policy = approval_setup
    context = FunctionInvocationContext(tool, {"command": "npm run test"})
    candidate = binding.candidate(context)
    assert candidate is not None
    assert await asyncio.to_thread(binding.service.remember, candidate, "EXACT_SESSION")
    middleware = ApprovalMiddleware(policy, bus, daa=binding)
    await middleware.close()
    rebuilt = DAABinding(binding.runtime, [tool], "rebuilt")
    other_session = DAABinding(binding.runtime, [tool], "other", session_id="session-b")
    assert await asyncio.to_thread(rebuilt.service.match, rebuilt.candidate(context)) == "HIT_ALLOW"
    assert await asyncio.to_thread(other_session.service.match, other_session.candidate(context)) == "MISS"
    assert len(await asyncio.to_thread(other_session.service.rules)) == 1


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
    middleware = ApprovalMiddleware(policy, bus, daa=binding)

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


@pytest.mark.parametrize("operation", ["match", "remember"])
async def test_changed_request_during_store_operation_requires_new_human_approval(
    approval_setup, monkeypatch, operation
):
    tool, binding, bus, policy = approval_setup
    context = FunctionInvocationContext(tool, {"command": "npm run test"})
    candidate = binding.candidate(context)
    assert candidate is not None
    if operation == "match":
        assert await asyncio.to_thread(binding.service.remember, candidate, "EXACT_PROJECT")
    original_match = binding.service.match
    original_remember = binding.service.remember

    def changed_match(current):
        result = original_match(current)
        context.arguments["command"] = "npm run build"
        return result

    def changed_remember(current, choice):
        result = original_remember(current, choice)
        context.arguments["command"] = "npm run build"
        return result

    if operation == "match":
        monkeypatch.setattr(binding.service, "match", changed_match)
    else:
        monkeypatch.setattr(binding.service, "remember", changed_remember)
    requests = []

    async def respond(event):
        requests.append(event.args["command"])
        await bus.publish(
            ApprovalResponse(
                request_id=event.request_id,
                approved=event.args["command"] == "npm run test",
                daa_choice="EXACT_PROJECT",
            )
        )

    middleware = ApprovalMiddleware(policy, bus, daa=binding)
    called = AsyncMock(spec=lambda: None)
    await bus.subscribe(ApprovalRequest, respond)
    try:
        await middleware.process(context, called)
        assert requests == (["npm run build"] if operation == "match" else ["npm run test", "npm run build"])
        called.assert_not_awaited()
        assert context.result == "Error: Tool execution was rejected by user."
        assert original_match(binding.candidate(context)) == "MISS"
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, respond)
