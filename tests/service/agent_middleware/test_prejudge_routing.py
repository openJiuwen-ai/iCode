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
        ("git 'status'", 1, 0),  # Valid quoted literal still ALLOWs
        ("ls *.py", 1, 0),  # Legacy readonly approval, now a built-in policy
        ("rm x", 0, 0),  # Explicit YAML BLOCK
        ("$'rm' x", 0, 0),
        ("$'\\x72m' x", 0, 0),
        ("rm x\n)", 0, 0),
        ("git\\\nstatus", 0, 1),  # Escaped newline is not static argv [git, status]
        ('unsafe_tool -f "pyproject."*', 0, 1),  # Concatenated glob is dynamic
        ("unsafe_tool -f {a,b}.txt", 0, 1),  # Brace expansion is dynamic
        ("echo ~", 0, 1),  # Unquoted tilde expansion
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
        "rules:\n"
        "  - id: block-rm\n    effect: block\n    target: command\n"
        "    match:\n      prefix_first: [rm]\n"
        "  - id: allow-status\n    effect: allow\n    target: command\n"
        "    match:\n      argv_exact: [git, status]\n"
        "  - id: bad-static-glob\n    effect: allow\n    target: command\n"
        '    match:\n      argv_exact: [unsafe_tool, "-f", "pyproject.*"]\n'
        "  - id: bad-brace\n    effect: allow\n    target: command\n"
        '    match:\n      argv_exact: [unsafe_tool, "-f", "{a,b}.txt"]\n'
        "  - id: bad-tilde\n    effect: allow\n    target: command\n"
        '    match:\n      argv_exact: [echo, "~"]\n',
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


@pytest.mark.parametrize(
    ("blocked_name", "command", "should_execute"),
    [
        ("rm", "ls *.py", True),
        ("ls", "ls *.py", False),
    ],
)
async def test_real_parser_block_only_keeps_readonly_and_block_precedence(
    tmp_path, monkeypatch, blocked_name, command, should_execute
):
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_bash")
    from chrys.service.approval.prejudge_v2 import load_policy

    path = tmp_path / "prejudge.yaml"
    path.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: only-block\n'
        "rules:\n  - id: block-command\n    effect: block\n    target: command\n"
        f"    match:\n      prefix_first: [{blocked_name}]\n",
        encoding="utf-8",
    )
    loaded = load_policy(path)
    assert len(loaded.rules) == 1
    assert loaded.readonly_rules
    monkeypatch.setattr(approval_module, "load_policy", lambda _: loaded)
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    bus = EventBus()
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]),
        bus,
        approval_mode=ApprovalMode.MANUAL,
    )
    context = FunctionInvocationContext(tool, {"command": command})
    executed = False

    async def execute():
        nonlocal executed
        executed = True

    try:
        await middleware.process(context, execute)
        assert executed is should_execute
    finally:
        await middleware.close()


async def test_bypass_skips_invalid_policy_loading_and_shadow(monkeypatch, caplog):
    def forbidden_loader(_):
        pytest.fail("BYPASS must not load a policy it will never evaluate")

    monkeypatch.setattr(approval_module, "load_policy", forbidden_loader)
    monkeypatch.setattr(
        approval_module,
        "route",
        lambda *a, **kw: pytest.fail("BYPASS must not route or shadow-log"),
    )
    bus = EventBus()
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]),
        bus,
        approval_mode=ApprovalMode.BYPASS,
    )
    executed = False

    async def execute():
        nonlocal executed
        executed = True

    try:
        with caplog.at_level(logging.INFO, logger=approval_module._prejudge_logger.name):
            await middleware.process(FunctionInvocationContext(tool, {"command": "rm x"}), execute)
        assert executed
        assert "shadow route=" not in caplog.text
    finally:
        await middleware.close()


async def test_session_policy_snapshot_shared_by_middleware_instances(monkeypatch):
    calls = []
    version = ["v1"]

    def load(_):
        calls.append(True)
        return Policy("enforce", version[0], ())

    monkeypatch.setattr(approval_module, "load_policy", load)
    bus = EventBus()
    policy = ApprovalPolicy(ApprovalConfig(default="require"))
    first = ApprovalMiddleware(policy, bus)
    # A later on-disk edit would produce v2 on reload. The next sub-agent
    # middleware must instead see the same session snapshot as the main agent.
    version[0] = "v2"
    second = ApprovalMiddleware(policy, bus)
    try:
        assert len(calls) == 1
        assert first._prejudge is second._prejudge
        assert second._prejudge.policy_id == "v1"
    finally:
        await first.close()
        await second.close()


async def test_bypass_to_manual_lazily_loads_one_session_snapshot(monkeypatch):
    calls = []

    def load(_):
        calls.append(True)
        return Policy("enforce", "switch", ())

    monkeypatch.setattr(approval_module, "load_policy", load)
    bus = EventBus()
    policy = ApprovalPolicy(ApprovalConfig(default="require"))
    first = ApprovalMiddleware(policy, bus, approval_mode=ApprovalMode.BYPASS)
    try:
        assert not calls
        first.set_approval_mode(ApprovalMode.MANUAL)
        assert len(calls) == 1
        second = ApprovalMiddleware(policy, bus, approval_mode=ApprovalMode.MANUAL)
        try:
            assert first._prejudge is second._prejudge
            assert len(calls) == 1
        finally:
            await second.close()
    finally:
        await first.close()


async def test_shadow_logs_bounded_command_text_and_spans(monkeypatch, caplog):
    monkeypatch.setattr(approval_module, "load_policy", lambda _: Policy("shadow", "pilot", ()))
    monkeypatch.setattr(
        approval_module,
        "route",
        lambda command, dialect, policy: PreJudgeResult(
            Route.DEFER, "unmatched_unit", (), ((0, 4),), policy.policy_id, "digest"
        ),
    )
    bus = EventBus()
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="skip"), tools=[tool]),
        bus,
    )
    command = "echo " + "x" * 510 + "hidden_suffix"
    executed = False

    async def execute():
        nonlocal executed
        executed = True

    try:
        with caplog.at_level(logging.INFO, logger=approval_module._prejudge_logger.name):
            await middleware.process(FunctionInvocationContext(tool, {"command": command}), execute)
        assert executed
        assert "shadow route=defer" in caplog.text
        assert "spans=((0, 4),)" in caplog.text
        assert "command='echo " in caplog.text
        assert "[truncated]" in caplog.text
        assert "hidden_suffix" not in caplog.text
    finally:
        await middleware.close()


async def test_human_edited_command_does_not_reenter_prejudge(monkeypatch):
    monkeypatch.setattr(approval_module, "load_policy", lambda _: Policy("enforce", "pilot", ()))
    routed = []

    def fake_route(command, dialect, policy):
        routed.append(command)
        return PreJudgeResult(
            Route.BLOCK if command == "rm x" else Route.DEFER,
            "matched_block" if command == "rm x" else "unmatched_unit",
            (),
            (),
            policy.policy_id,
            "digest",
        )

    monkeypatch.setattr(approval_module, "route", fake_route)
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    bus = EventBus()

    async def approve(req: ApprovalRequest):
        await bus.publish(ApprovalResponse(request_id=req.request_id, approved=True, modified_args={"command": "rm x"}))

    await bus.subscribe(ApprovalRequest, approve)
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]),
        bus,
        approval_mode=ApprovalMode.MANUAL,
    )
    ran = False

    async def execute():
        nonlocal ran
        ran = True

    try:
        await middleware.process(FunctionInvocationContext(tool, {"command": "unknown_tool"}), execute)
        assert routed == ["unknown_tool"]
        assert ran
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)
