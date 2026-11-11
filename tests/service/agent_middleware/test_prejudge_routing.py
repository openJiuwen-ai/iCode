# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Structured pre-judge middleware decisions without importing optional parsers."""

from __future__ import annotations

import logging
from types import MappingProxyType

import pytest
from pydantic import BaseModel

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse, InvocationToolCallResult
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_kinds import KIND_SHELL, set_tool_kind
from chrys.foundation.tool_result_metadata import TOOL_ERROR_KIND_METADATA_KEY
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.kernel.tools import FunctionTool
from chrys.service.agent_middleware.control import approval as approval_module
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.agent_middleware.events.tool_events import ToolEventMiddleware
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.approval.prejudge_v2 import Policy, PreJudgeResult, Route
from chrys.service.profiles.agents.schema import ApprovalConfig
from tests.support.event_capture import capture_events


@pytest.mark.parametrize(
    ("mode", "suggestion", "expected_executions", "expected_requests"),
    [
        ("enforce", Route.BLOCK, 0, 0),
        ("enforce", Route.ALLOW, 1, 0),
        ("enforce", Route.DEFER, 1, 1),
        ("shadow", Route.BLOCK, 1, 0),
    ],
)
async def test_shell_routes_do_not_bypass_or_mutate_legacy(
    monkeypatch, mode, suggestion, expected_executions, expected_requests
):
    monkeypatch.setattr(approval_module, "load_policy", lambda _: Policy(mode, "pilot", ()))
    monkeypatch.setattr(
        approval_module,
        "route",
        lambda cmd, dialect, policy: PreJudgeResult(suggestion, "test", (), (), policy.policy_id, "digest"),
    )
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    bus = EventBus()
    requests = []

    async def approve(req: ApprovalRequest):
        requests.append(req)
        await bus.publish(ApprovalResponse(request_id=req.request_id, approved=True))

    await bus.subscribe(ApprovalRequest, approve)
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]),
        bus,
        approval_mode=ApprovalMode.MANUAL,
    )
    context = FunctionInvocationContext(tool, {"command": "ls"})
    executed = 0

    async def execute():
        nonlocal executed
        executed += 1

    try:
        await middleware.process(context, execute)
        assert executed == expected_executions
        assert len(requests) == expected_requests
        if suggestion is Route.BLOCK and mode == "enforce":
            assert context.result.startswith("Error:")
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


async def test_non_shell_tool_never_invokes_prejudge(monkeypatch):
    monkeypatch.setattr(approval_module, "load_policy", lambda _: Policy("enforce", "pilot", ()))

    def forbidden(*args, **kwargs):
        pytest.fail("Non-shell call must not enter pre-judge")

    monkeypatch.setattr(approval_module, "route", forbidden)
    tool = FunctionTool(name="other_tool")
    context = FunctionInvocationContext(tool, {})
    bus = EventBus()
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="skip"), tools=[tool]),
        bus,
    )
    executed = False

    async def execute():
        nonlocal executed
        executed = True

    try:
        await middleware.process(context, execute)
        assert executed
    finally:
        await middleware.close()


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        ("prejudge", "prejudge_blocked"),
        ("approval_invalidated", "approval_invalidated"),
    ],
)
async def test_middleware_rejection_source_is_persisted(monkeypatch, source, expected_kind):
    """Exercise real ApprovalMiddleware before ToolEventMiddleware persistence."""
    monkeypatch.setattr(approval_module, "load_policy", lambda _: Policy("enforce", "pilot", ()))
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    approval = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]),
        bus,
        approval_mode=ApprovalMode.MANUAL,
    )
    event_mw = ToolEventMiddleware(
        bus,
        session_id="test",
        origin=InvocationOrigin("turn", "test", "turn-prejudge", None),
    )
    context = FunctionInvocationContext(tool, {"command": "rm fixture"})
    executed = False

    async def execute():
        nonlocal executed
        executed = True

    if source == "prejudge":
        monkeypatch.setattr(
            approval_module,
            "route",
            lambda cmd, dialect, policy: PreJudgeResult(
                Route.BLOCK, "matched_block", (), (), policy.policy_id, "digest"
            ),
        )
    else:
        monkeypatch.setattr(
            approval_module,
            "route",
            lambda cmd, dialect, policy: PreJudgeResult(
                Route.DEFER, "unmatched_unit", (), (), policy.policy_id, "digest"
            ),
        )

        async def reply(request: ApprovalRequest) -> None:
            await bus.publish(
                ApprovalResponse(
                    request_id=request.request_id,
                    approved=True,
                    modified_args={"command": "echo approved"},
                )
            )

        await bus.subscribe(ApprovalRequest, reply)

        async def rewrite_edited_command(**kwargs) -> bool:
            kwargs["context"].arguments["command"] = "echo rewritten"
            return False

        monkeypatch.setattr(approval_module, "apply_before_tool_hooks", rewrite_edited_command)

    try:
        await event_mw.process(context, lambda: approval.process(context, execute))
        assert not executed
        assert len(results) == 1
        assert results[0].metadata[TOOL_ERROR_KIND_METADATA_KEY] == expected_kind
        assert "approval" not in results[0].metadata
    finally:
        await approval.close()


class _ShellArgs(BaseModel):
    command: str


@pytest.mark.parametrize(
    "arguments",
    [
        _ShellArgs(command="rm fixture"),
        MappingProxyType({"command": "rm fixture"}),
        '{"command": "rm fixture"}',
    ],
    ids=["pydantic_model", "read_only_mapping", "json_string"],
)
async def test_enforce_block_reads_non_dict_shell_arguments(monkeypatch, arguments):
    """BLOCK rules see the same command text the approval request would show."""
    monkeypatch.setattr(approval_module, "load_policy", lambda _: Policy("enforce", "pilot", ()))
    routed: list[str] = []

    def block(cmd, dialect, policy):
        routed.append(cmd)
        return PreJudgeResult(Route.BLOCK, "matched_block", (), (), policy.policy_id, "digest")

    monkeypatch.setattr(approval_module, "route", block)
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    bus = EventBus()
    requests: list[ApprovalRequest] = []

    async def reject(req: ApprovalRequest):
        # Answer instead of hanging if the BLOCK rule was skipped.
        requests.append(req)
        await bus.publish(ApprovalResponse(request_id=req.request_id, approved=False))

    await bus.subscribe(ApprovalRequest, reject)
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]),
        bus,
        approval_mode=ApprovalMode.MANUAL,
    )
    context = FunctionInvocationContext(tool, arguments)
    executed = False

    async def execute():
        nonlocal executed
        executed = True

    try:
        await middleware.process(context, execute)
        assert routed == ["rm fixture"]
        assert requests == []
        assert not executed
        assert context.result == "Error: Command blocked by structured pre-judge."
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, reject)


async def test_enforce_without_command_text_logs_defer_and_asks(monkeypatch, caplog):
    monkeypatch.setattr(approval_module, "load_policy", lambda _: Policy("enforce", "pilot", ()))

    def forbidden(*args, **kwargs):
        pytest.fail("No command text must not be routed")

    monkeypatch.setattr(approval_module, "route", forbidden)
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    bus = EventBus()
    requests: list[ApprovalRequest] = []

    async def approve(req: ApprovalRequest):
        requests.append(req)
        await bus.publish(ApprovalResponse(request_id=req.request_id, approved=True))

    await bus.subscribe(ApprovalRequest, approve)
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]),
        bus,
        approval_mode=ApprovalMode.MANUAL,
    )
    context = FunctionInvocationContext(tool, "not json")
    executed = False

    async def execute():
        nonlocal executed
        executed = True

    try:
        with caplog.at_level(logging.INFO, logger=approval_module.__name__ + ".prejudge"):
            await middleware.process(context, execute)
        assert "enforce route=defer reason=no_command_text policy=pilot" in caplog.messages
        assert len(requests) == 1
        assert executed
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


@pytest.mark.parametrize(
    ("command", "expected_executions", "expected_requests"),
    [
        ("git status", 1, 0),
        ("ls *.py", 1, 0),  # Legacy readonly approval, now a built-in policy
        ("rm x", 0, 0),  # Explicit YAML BLOCK
        ("git\\\nstatus", 0, 1),  # Escaped newline is not static argv [git, status]
        ('unsafe_tool -f "pyproject."*', 0, 1),  # Concatenated glob is dynamic
    ],
)
async def test_real_parser_yaml_policy_and_approval_integration(
    tmp_path, monkeypatch, command, expected_executions, expected_requests
):
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_bash")
    from chrys.service.approval.prejudge_v2 import load_policy

    policy_file = tmp_path / "prejudge.yaml"
    policy_file.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: end-to-end\n'
        'rules:\n'
        '  - id: block-rm\n    effect: block\n    target: command\n'
        '    match:\n      prefix_first: [rm]\n'
        '  - id: allow-status\n    effect: allow\n    target: command\n'
        '    match:\n      argv_exact: [git, status]\n'
        '  - id: bad-static-glob\n    effect: allow\n    target: command\n'
        '    match:\n      argv_exact: [unsafe_tool, "-f", "pyproject.*"]\n',
        encoding="utf-8",
    )
    policy = load_policy(policy_file)
    monkeypatch.setattr(approval_module, "load_policy", lambda _: policy)
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    bus = EventBus()
    requests: list[ApprovalRequest] = []

    async def reject(request: ApprovalRequest) -> None:
        requests.append(request)
        await bus.publish(ApprovalResponse(request_id=request.request_id, approved=False))

    await bus.subscribe(ApprovalRequest, reject)
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]),
        bus,
        approval_mode=ApprovalMode.MANUAL,
    )
    context = FunctionInvocationContext(tool, {"command": command})
    executions = 0

    async def execute() -> None:
        nonlocal executions
        executions += 1

    try:
        await middleware.process(context, execute)
        assert executions == expected_executions
        assert len(requests) == expected_requests
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, reject)
