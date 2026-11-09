# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Command grants survive incidental changes while binding the executing shell."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, create_autospec

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse, ApprovalReviewed
from chrys.foundation.models.approval_reuse import ApprovalReuseOffer
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.judge import ApprovalJudge, JudgeVerdict
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.approval.reuse_binding import ApprovalReuseBinding
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


def reuse_candidate(binding, context):
    return binding.prepare(context, reusable=True).candidate


def shell_binding(runtime, *, shell=None):
    tools = ShellTools(runtime, shell=shell).tools()
    tool = tools[0]
    return tool, ApprovalReuseBinding(runtime, tools)


@pytest.mark.parametrize("choice", ["EXACT_SESSION", "EXACT_PROJECT", "PREFIX_SESSION", "PREFIX_PROJECT"])
async def test_human_command_grant_survives_rebuild_and_environment_change(runtime, monkeypatch, choice):
    monkeypatch.setenv("APPROVAL_REUSE_TEST_ENV", "before")
    tool, binding = shell_binding(runtime)
    bus = EventBus()
    policy = ApprovalPolicy(ApprovalConfig(default="require", overrides={}))
    requests = []

    async def approve(event: ApprovalRequest):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True, remember_choice=choice))

    await bus.subscribe(ApprovalRequest, approve)
    called = AsyncMock(spec=lambda: None)
    middleware = ApprovalMiddleware(policy, bus, session_id=runtime.session_id, reuse=binding)
    args = {"command": "npm run test", "reason": "first", "timeout": 30, "max_output_tokens": 8000}
    try:
        await middleware.process(FunctionInvocationContext(tool, args), called)
        assert len(requests) == len(binding.service.rules()) == 1
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)

    monkeypatch.setenv("APPROVAL_REUSE_TEST_ENV", "after")
    runtime = replace(runtime, session_id="session-b" if choice.endswith("PROJECT") else runtime.session_id)
    rebuilt_tool, rebuilt = shell_binding(runtime)
    middleware = ApprovalMiddleware(policy, bus, session_id=runtime.session_id, reuse=rebuilt)

    async def decline(event: ApprovalRequest):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=False))

    await bus.subscribe(ApprovalRequest, decline)
    try:
        changed = {**args, "reason": "rerun checks", "timeout": 90, "max_output_tokens": 1000}
        if choice.startswith("PREFIX"):
            changed["command"] += " -- --runInBand"
        await middleware.process(FunctionInvocationContext(rebuilt_tool, changed), called)
        assert len(requests) == 1
        assert called.await_count == 2
        assert middleware.drain_decisions()[-1]["status"] == "reuse_approved"

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
    approved = reuse_candidate(binding, FunctionInvocationContext(tool, args))
    assert approved and binding.service.remember(approved, choice)
    if change == "cwd":
        runtime = replace(runtime, cwd=runtime.cwd + "/other")
    else:
        active_shell = replace(
            active_shell,
            **{change: {"name": "zsh", "path": active_shell.path + ".other", "args": ["-l", "-c"]}[change]},
        )
    changed_tool, changed_binding = shell_binding(runtime, shell=active_shell)
    assert not changed_binding.service.match(
        reuse_candidate(changed_binding, FunctionInvocationContext(changed_tool, args))
    )


@pytest.mark.parametrize(("source", "target"), [("cmd", "pwsh"), ("bash", "pwsh"), ("pwsh", "cmd")])
def test_session_command_grant_cannot_cross_registered_shells(runtime, source, target):
    tools = [
        ShellTools(runtime, shell=replace(runtime.platform.shell, name=name)).tools()[0] for name in (source, target)
    ]
    binding = ApprovalReuseBinding(runtime, tools)
    args = {"command": "sc query foo", "reason": "test"}
    approved = reuse_candidate(binding, FunctionInvocationContext(tools[0], args))
    assert approved and binding.service.remember(approved, "EXACT_SESSION")
    assert binding.service.match(reuse_candidate(binding, FunctionInvocationContext(tools[0], args)))
    assert not binding.service.match(reuse_candidate(binding, FunctionInvocationContext(tools[1], args)))


@pytest.mark.parametrize("human_remembers", [False, True])
async def test_judge_review_preserves_explicit_human_remember_choice(runtime, monkeypatch, human_remembers):
    tool, binding = shell_binding(runtime)
    bus = EventBus()
    judge = create_autospec(ApprovalJudge, instance=True)
    judge.evaluate.return_value = JudgeVerdict(approved=True, reason="reviewed")
    trace = create_autospec(ApprovalTrace, instance=True)
    monkeypatch.setattr(ApprovalTrace, "open", create_autospec(ApprovalTrace.open, return_value=trace))

    async def remember(event: ApprovalReviewed):
        if human_remembers:
            await bus.publish(
                ApprovalResponse(request_id=event.request_id, approved=True, remember_choice="EXACT_PROJECT")
            )

    await bus.subscribe(ApprovalReviewed, remember)
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require", overrides={})),
        bus,
        session_id=runtime.session_id,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
        reuse=binding,
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
            judge_audit=None,
        )
        assert bool(binding.service.match(reuse_candidate(binding, context))) is human_remembers
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalReviewed, remember)


async def test_compound_command_can_be_remembered_for_the_session(runtime):
    tool, binding = shell_binding(runtime)
    bus, requests = EventBus(), []
    command = "npm test && npm run lint"

    async def approve(event: ApprovalRequest):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True, remember_choice="EXACT_SESSION"))

    await bus.subscribe(ApprovalRequest, approve)
    called = AsyncMock(spec=lambda: None)
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require")), bus, session_id=runtime.session_id, reuse=binding
    )
    try:
        for _ in range(2):
            await middleware.process(FunctionInvocationContext(tool, {"command": command}), called)
        assert called.await_count == 2
        assert [event.reuse_offer for event in requests] == [ApprovalReuseOffer("command", (command,))]
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)
