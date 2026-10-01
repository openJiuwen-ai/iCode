# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Command grants survive incidental changes while binding the executing shell."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, create_autospec

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse, ApprovalReviewed
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.daa_binding import DAABinding
from chrys.service.approval.judge import ApprovalJudge, JudgeVerdict
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.tools.builtins.shell import ShellTools
from chrys.service.trajectory.approvals import ApprovalDecider, ApprovalTrace


@pytest.fixture
def runtime(tmp_path):
    runtime = SessionEnvironment.capture("session-a", Workspace.from_cwd(str(tmp_path)))
    return replace(
        runtime,
        platform=replace(
            runtime.platform, config_dir=tmp_path / "config", shell=replace(runtime.platform.shell, name="bash")
        ),
    )


def shell_binding(runtime, *, shell=None, profile="profile-a"):
    tools = ShellTools(runtime, shell=shell).tools()
    tool = tools[0]
    return tool, DAABinding(runtime, tools, profile)


@pytest.mark.parametrize("choice", ["EXACT_SESSION", "EXACT_PROJECT", "PREFIX_SESSION", "PREFIX_PROJECT"])
async def test_human_command_grant_survives_rebuild_and_environment_change(runtime, monkeypatch, choice):
    monkeypatch.setenv("DAA_REUSE_TEST_ENV", "before")
    tool, binding = shell_binding(runtime)
    bus = EventBus()
    policy = ApprovalPolicy(ApprovalConfig(default="require", overrides={}))
    requests = []

    async def approve(event: ApprovalRequest):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True, daa_choice=choice))

    await bus.subscribe(ApprovalRequest, approve)
    called = AsyncMock(spec=lambda: None)
    middleware = ApprovalMiddleware(policy, bus, session_id=runtime.session_id, daa=binding)
    args = {"command": "npm run test", "reason": "first", "timeout": 30, "max_tokens": 8000}
    try:
        await middleware.process(FunctionInvocationContext(tool, args), called)
        assert len(requests) == len(binding.service.rules()) == 1
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)

    monkeypatch.setenv("DAA_REUSE_TEST_ENV", "after")
    runtime = replace(runtime, session_id="session-b" if choice.endswith("PROJECT") else runtime.session_id)
    rebuilt_tool, rebuilt = shell_binding(runtime, profile="profile-b")
    middleware = ApprovalMiddleware(policy, bus, session_id=runtime.session_id, daa=rebuilt)

    async def decline(event: ApprovalRequest):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=False))

    await bus.subscribe(ApprovalRequest, decline)
    try:
        changed = {**args, "reason": "rerun checks", "timeout": 90, "max_tokens": 1000}
        if choice.startswith("PREFIX"):
            changed["command"] += " -- --runInBand"
        await middleware.process(FunctionInvocationContext(rebuilt_tool, changed), called)
        assert len(requests) == 1
        assert called.await_count == 2
        assert middleware.drain_decisions()[-1]["status"] == "daa_approved"

        await middleware.process(
            FunctionInvocationContext(rebuilt_tool, {**changed, "command": "npm run build"}), called
        )
        assert len(requests) == 2
        assert called.await_count == 2
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, decline)


@pytest.mark.parametrize("choice", ["EXACT_PROJECT", "PREFIX_SESSION", "PREFIX_PROJECT"])
@pytest.mark.parametrize("change", ["name", "path", "args", "cwd"])
def test_command_grant_binds_active_shell_and_cwd(runtime, choice, change):
    # Exercise an extra shell: runtime.platform.shell is not the executing one.
    active_shell = replace(runtime.platform.shell, path=runtime.platform.shell.path + ".extra", args=["-c"])
    tool, binding = shell_binding(runtime, shell=active_shell)
    args = {"command": "npm run test", "reason": "first"}
    approved = binding.candidate(FunctionInvocationContext(tool, args))
    assert approved and binding.service.remember(approved, choice)
    if change == "cwd":
        runtime = replace(runtime, cwd=runtime.cwd + "/other")
    else:
        active_shell = replace(
            active_shell,
            **{change: {"name": "zsh", "path": active_shell.path + ".other", "args": ["-l", "-c"]}[change]},
        )
    changed_tool, changed_binding = shell_binding(runtime, shell=active_shell)
    assert (
        changed_binding.service.match(changed_binding.candidate(FunctionInvocationContext(changed_tool, args)))
        == "MISS"
    )


@pytest.mark.parametrize("approved", [False, True])
async def test_daa_response_cannot_be_changed_after_publication(runtime, approved):
    tool, binding = shell_binding(runtime)
    bus = EventBus()
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require", overrides={})),
        bus,
        session_id=runtime.session_id,
        daa=binding,
    )
    called = AsyncMock(spec=lambda: None)

    async def answer(event: ApprovalRequest):
        response = ApprovalResponse(request_id=event.request_id, approved=approved)
        await bus.publish(response)
        # The request handler still owns this object while the middleware's
        # response future has settled but its execution has not resumed.
        response.approved = True
        response.daa_choice = "EXACT_PROJECT"

    await bus.subscribe(ApprovalRequest, answer)
    try:
        await middleware.process(FunctionInvocationContext(tool, {"command": "npm run test"}), called)
        assert called.await_count == int(approved)
        assert binding.service.rules() == []
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, answer)


@pytest.mark.parametrize(("source", "target"), [("cmd", "pwsh"), ("bash", "pwsh"), ("pwsh", "cmd")])
def test_session_command_grant_cannot_cross_registered_shells(runtime, source, target):
    tools = [
        ShellTools(runtime, shell=replace(runtime.platform.shell, name=name)).tools()[0] for name in (source, target)
    ]
    binding = DAABinding(runtime, tools, "profile")
    args = {"command": "sc query foo", "reason": "test"}
    approved = binding.candidate(FunctionInvocationContext(tools[0], args))
    assert approved and binding.service.remember(approved, "EXACT_SESSION")
    assert binding.service.match(binding.candidate(FunctionInvocationContext(tools[0], args))) == "HIT_ALLOW"
    assert binding.service.match(binding.candidate(FunctionInvocationContext(tools[1], args))) == "MISS"


@pytest.mark.parametrize("human_remembers", [False, True])
async def test_judge_review_preserves_explicit_human_daa_choice(runtime, monkeypatch, human_remembers):
    tool, binding = shell_binding(runtime)
    bus = EventBus()
    judge = create_autospec(ApprovalJudge, instance=True)
    judge.evaluate.return_value = JudgeVerdict(approved=True, reason="reviewed")
    trace = create_autospec(ApprovalTrace, instance=True)
    monkeypatch.setattr(ApprovalTrace, "open", create_autospec(ApprovalTrace.open, return_value=trace))

    async def remember(event: ApprovalReviewed):
        if human_remembers:
            await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True, daa_choice="EXACT_PROJECT"))

    await bus.subscribe(ApprovalReviewed, remember)
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require", overrides={})),
        bus,
        session_id=runtime.session_id,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
        daa=binding,
    )
    context = FunctionInvocationContext(tool, {"command": "npm run test", "reason": "test"})
    called = AsyncMock(spec=lambda: None)
    try:
        await middleware.process(context, called)
        called.assert_awaited_once()
        judge.evaluate.assert_awaited_once()
        trace.resolved.assert_awaited_once_with(
            approved=True,
            decider=ApprovalDecider.USER if human_remembers else ApprovalDecider.JUDGE,
            reason_code="",
            arguments_modified=False,
        )
        assert binding.service.match(binding.candidate(context)) == ("HIT_ALLOW" if human_remembers else "MISS")
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalReviewed, remember)


@pytest.mark.parametrize("enabled", [False, True])
async def test_live_request_changes_require_reapproval_only_with_daa(runtime, enabled):
    tool, binding = shell_binding(runtime)
    bus = EventBus()
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require", overrides={})),
        bus,
        session_id=runtime.session_id,
        daa=binding if enabled else None,
    )
    context = FunctionInvocationContext(tool, {"command": "npm run test", "reason": "test"})
    requested_commands = []

    async def approve(event: ApprovalRequest):
        requested_commands.append(event.args["command"])
        context.arguments["command"] = "npm run build"
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True, daa_choice="EXACT_PROJECT"))

    await bus.subscribe(ApprovalRequest, approve)
    called = AsyncMock(spec=lambda: None)
    try:
        await middleware.process(context, called)
        called.assert_awaited_once()
        assert requested_commands == (["npm run test", "npm run build"] if enabled else ["npm run test"])
        assert binding.service.match(binding.candidate(context)) == ("HIT_ALLOW" if enabled else "MISS")
        original = FunctionInvocationContext(tool, {"command": "npm run test", "reason": "test"})
        assert binding.service.match(binding.candidate(original)) == "MISS"
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)
