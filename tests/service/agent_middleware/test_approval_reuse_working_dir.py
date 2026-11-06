# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit shell directories cannot reuse or create remembered approvals."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.policy import ApprovalPolicy
from chrys.service.approval.reuse_binding import ApprovalReuseBinding
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.tools.builtins.shell import ShellTools


@pytest.fixture
def setup(tmp_path):
    runtime = SessionEnvironment.capture("session-a", Workspace.from_cwd(str(tmp_path)))
    # This event-only harness exercises the existing POSIX PREFIX choices too;
    # real engine tests execute the actual platform shell.
    runtime = replace(
        runtime,
        platform=replace(
            runtime.platform, config_dir=tmp_path / "config", shell=replace(runtime.platform.shell, name="bash")
        ),
    )
    tools = ShellTools(runtime).tools()
    tool = next(t for t in tools if t.name == runtime.platform.shell.name)
    binding = ApprovalReuseBinding(runtime, tools)
    bus = EventBus()
    policy = ApprovalPolicy(ApprovalConfig(default="require", overrides={}))
    return runtime, tool, binding, bus, policy


def reuse_candidate(binding, context):
    return binding.prepare(context, reusable=True).candidate


def invocation(tool, command="npm run dev"):
    return FunctionInvocationContext(tool, {"command": command, "reason": "test"})


@pytest.mark.parametrize("command", ["git reset --hard", "rm -rf .", "make clean", "./deploy.sh"])
@pytest.mark.parametrize("opaque_context", [False, True])
async def test_session_approved_command_requires_approval_with_working_dir(setup, command, opaque_context):
    runtime, tool, binding, bus, policy = setup
    middleware = ApprovalMiddleware(
        policy, bus, session_id=runtime.session_id, workspace_cwd=runtime.cwd, reuse=binding
    )
    requests = []

    async def respond(event: ApprovalRequest):
        requests.append(event)
        await bus.publish(
            ApprovalResponse(request_id=event.request_id, approved=len(requests) == 1, remember_choice="EXACT_SESSION")
        )

    called = AsyncMock(spec=lambda: None)
    await bus.subscribe(ApprovalRequest, respond)
    try:
        await middleware.process(invocation(tool, command), called)
        assert len(binding.service.rules()) == 1
        for working_dir in (runtime.cwd + "/main", "~", "../main"):
            redirected = invocation(tool, command)
            redirected.arguments["workdir"] = working_dir
            if opaque_context:
                redirected.kwargs["host_context"] = object()
            await middleware.process(redirected, called)
            assert redirected.result == "Error: Tool execution was rejected by user."
        assert len(requests) == 4
        assert all(not request.reuse_offer for request in requests[1:])
        called.assert_awaited_once()
        await middleware.process(invocation(tool, command), called)
        assert called.await_count == 2
        assert len(requests) == 4
        assert middleware.drain_decisions()[-1]["status"] == "reuse_approved"
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, respond)


@pytest.mark.parametrize("choice", ["EXACT_SESSION", "EXACT_PROJECT", "PREFIX_SESSION", "PREFIX_PROJECT"])
async def test_explicit_working_dir_approval_never_mints_reuse(setup, choice):
    runtime, tool, binding, bus, policy = setup
    middleware = ApprovalMiddleware(
        policy, bus, session_id=runtime.session_id, workspace_cwd=runtime.cwd, reuse=binding
    )
    requests = []

    async def approve(event: ApprovalRequest):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True, remember_choice=choice))

    called = AsyncMock(spec=lambda: None)
    await bus.subscribe(ApprovalRequest, approve)
    try:
        for working_dir in (runtime.cwd, runtime.cwd, runtime.cwd + "/other"):
            ctx = invocation(tool, "git reset --hard")
            ctx.arguments["workdir"] = working_dir
            await middleware.process(ctx, called)
            assert not binding.service.rules()
        assert len(requests) == called.await_count == 3
        assert all(not request.reuse_offer for request in requests)
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


@pytest.mark.parametrize("opaque_context", ["none", "kwargs", "arguments"])
def test_working_dir_cannot_bypass_reuse_guard_with_opaque_arguments(setup, opaque_context):
    runtime, tool, binding, _, _ = setup
    ctx = FunctionInvocationContext(tool, {"command": "git reset --hard"})
    first = reuse_candidate(binding, ctx)
    assert first and binding.service.remember(first, "EXACT_SESSION")
    ctx.arguments["workdir"] = runtime.cwd + "/other"
    if opaque_context == "kwargs":
        ctx.kwargs["host_context"] = object()
    elif opaque_context == "arguments":
        ctx.arguments["reason"] = object()
    current = reuse_candidate(binding, ctx)
    assert not binding.service.match(current)
    assert current is None


@pytest.mark.parametrize("working_dir", [None, ""])
def test_empty_working_dir_is_the_session_folder(setup, working_dir):
    _, tool, binding, _, _ = setup
    remembered = reuse_candidate(binding, invocation(tool, "git status"))
    assert remembered and binding.service.remember(remembered, "EXACT_SESSION")
    ctx = invocation(tool, "git status")
    ctx.arguments["workdir"] = working_dir
    assert binding.service.match(reuse_candidate(binding, ctx))
