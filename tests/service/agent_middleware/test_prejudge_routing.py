# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Structured pre-judge middleware decisions without importing optional parsers."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, create_autospec

import pytest
from pydantic import BaseModel

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse, ApprovalReviewed, InvocationToolCallResult
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.tool_kinds import KIND_SHELL, set_tool_kind
from chrys.foundation.tool_result_metadata import TOOL_ERROR_KIND_METADATA_KEY
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.kernel.tools import FunctionTool
from chrys.service.agent_middleware.control import approval as approval_module
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.agent_middleware.events.tool_events import ToolEventMiddleware
from chrys.service.approval import prejudge_v2
from chrys.service.approval.judge import ApprovalJudge, JudgeVerdict
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.approval.prejudge_v2 import Matcher, Policy, PreJudgeResult, Route, Rule, load_policy, route
from chrys.service.approval.reuse_binding import ApprovalReuseBinding
from chrys.service.profiles.agents.schema import ApprovalConfig
from tests.support.event_capture import capture_events
from tests.support.prejudge import bound_bash_tool, require_grammars, set_fixture_search_path


@pytest.mark.parametrize(
    ("mode", "suggestion", "expected_executions", "expected_requests"),
    [
        ("enforce", Route.ALLOW, 1, 0),
        ("enforce", Route.DEFER, 1, 1),
        ("shadow", Route.ALLOW, 1, 1),
        ("shadow", Route.DEFER, 1, 1),
    ],
)
async def test_shell_allow_and_defer_follow_existing_approval(
    tmp_path, monkeypatch, mode, suggestion, expected_executions, expected_requests
):
    monkeypatch.setattr(approval_module, "load_policy", lambda _: Policy(mode, "pilot", ()))
    monkeypatch.setattr(
        approval_module,
        "route",
        lambda cmd, dialect, policy: PreJudgeResult(suggestion, "test", (), (), policy.policy_id, "digest"),
    )
    await set_fixture_search_path(monkeypatch, tmp_path)
    tool = bound_bash_tool()
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
    context = FunctionInvocationContext(tool, {"command": "unknown_tool"})
    executed = 0

    async def execute():
        nonlocal executed
        executed += 1

    try:
        await middleware.process(context, execute)
        assert executed == expected_executions
        assert len(requests) == expected_requests
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


async def test_approval_invalidation_source_is_persisted(monkeypatch):
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

    monkeypatch.setattr(
        approval_module,
        "route",
        lambda cmd, dialect, policy: PreJudgeResult(Route.DEFER, "unmatched_unit", (), (), policy.policy_id, "digest"),
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
        assert results[0].metadata[TOOL_ERROR_KIND_METADATA_KEY] == "approval_invalidated"
        assert "approval" not in results[0].metadata
    finally:
        await approval.close()
        await bus.unsubscribe(ApprovalRequest, reply)


class _ShellArgs(BaseModel):
    command: str


@pytest.mark.parametrize(
    "arguments",
    [
        _ShellArgs(command="unknown_tool fixture"),
        MappingProxyType({"command": "unknown_tool fixture"}),
        '{"command": "unknown_tool fixture"}',
    ],
    ids=["pydantic_model", "read_only_mapping", "json_string"],
)
async def test_enforce_defer_reads_non_dict_shell_arguments(monkeypatch, arguments):
    """The route and ordinary approval request see the same command text."""
    monkeypatch.setattr(approval_module, "load_policy", lambda _: Policy("enforce", "pilot", ()))
    routed: list[str] = []

    def defer(cmd, dialect, policy):
        routed.append(cmd)
        return PreJudgeResult(Route.DEFER, "unmatched_unit", (), (), policy.policy_id, "digest")

    monkeypatch.setattr(approval_module, "route", defer)
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    bus = EventBus()
    requests: list[ApprovalRequest] = []

    async def reject(req: ApprovalRequest):
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
        assert routed == ["unknown_tool fixture"]
        assert len(requests) == 1
        assert requests[0].args == {"command": "unknown_tool fixture"}
        assert not executed
        assert context.metadata[approval_module._REJECTION_SOURCE_KEY] == "user"
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
        ("ls *.py", 1, 0),  # DEFER retains the existing readonly shortcut
        ("rm x", 0, 1),  # Unmatched commands enter ordinary approval
        ("$'rm' x", 0, 1),
        ("$'\\x72m' x", 0, 1),
        ("rm x\n)", 0, 1),
        ("git\\\nstatus", 0, 1),  # Escaped newline is not static argv [git, status]
        ('unsafe_tool -f "pyproject."*', 0, 1),  # Concatenated glob is dynamic
        ("unsafe_tool -f {a,b}.txt", 0, 1),  # Brace expansion is dynamic
        ("echo ~", 1, 0),  # Structured DEFER; legacy readonly approval still applies
    ],
)
async def test_real_parser_yaml_policy_and_approval_integration(
    tmp_path, monkeypatch, command, expected_executions, expected_requests
):
    require_grammars("tree_sitter", "tree_sitter_bash")
    policy_file = tmp_path / "prejudge.yaml"
    policy_file.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: end-to-end\n'
        "rules:\n"
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
    await set_fixture_search_path(monkeypatch, tmp_path)
    tool = bound_bash_tool()
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
        if command in ("ls *.py", "echo ~"):
            assert middleware._decisions[-1]["status"] == "auto_approved"
        if expected_requests:
            assert context.metadata[approval_module._REJECTION_SOURCE_KEY] == "user"
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, reject)


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("matches", [False, True], ids=["unmatched", "matching"])
@pytest.mark.parametrize("path", ["policy-auto", "legacy-readonly", "daa", "auto-judge"])
async def test_decoded_target_changes_only_enforce_approval(tmp_path, monkeypatch, caplog, mode, matches, path):
    require_grammars("tree_sitter", "tree_sitter_bash")
    executable = "cat" if path == "legacy-readonly" else "review_fixture_tool"
    command = f"{executable} .e'n'v"
    policy_file = tmp_path / "prejudge.yaml"
    rules = (
        "rules:\n  - id: fixture\n    effect: allow\n    target: command\n"
        f"    match:\n      argv_first: [{executable}]\n"
        if matches
        else "rules: []\n"
    )
    policy_file.write_text(f'schema_version: 1\nmode: "{mode}"\npolicy_id: fixture\n{rules}', encoding="utf-8")
    policy = load_policy(policy_file)
    monkeypatch.setattr(approval_module, "load_policy", lambda _: policy)
    tool = bound_bash_tool()
    context = FunctionInvocationContext(tool, {"command": command, "reason": "Classify an inert test string"})
    require = path in {"legacy-readonly", "daa"}
    config = ApprovalPolicy(
        ApprovalConfig(default="require" if require else "auto", overrides={} if require else {"shell": "auto"}),
        tools=[tool],
    )
    reuse = None
    if path == "daa":
        runtime = SessionEnvironment.capture("decoded-target", Workspace.from_cwd(str(tmp_path)))
        reuse = ApprovalReuseBinding(runtime, [tool])
        prepared = reuse.prepare(context, reusable=True)
        assert prepared.candidate is not None
        assert reuse.service.remember(prepared.candidate, "EXACT_SESSION")
    judge = create_autospec(ApprovalJudge, instance=True) if path == "auto-judge" else None
    if judge is not None:
        judge.evaluate.return_value = JudgeVerdict(approved=False, reason="fixture rejection")
    bus = EventBus()
    requests = []

    async def answer(request: ApprovalRequest) -> None:
        requests.append(request)
        if judge is None:
            await bus.publish(ApprovalResponse(request_id=request.request_id, approved=False))

    async def reject_review(event: ApprovalReviewed) -> None:
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=False))

    await bus.subscribe(ApprovalRequest, answer)
    await bus.subscribe(ApprovalReviewed, reject_review)
    middleware = ApprovalMiddleware(
        config,
        bus,
        reuse=reuse,
        approval_judge=judge,
        approval_mode=ApprovalMode.AUTO if judge is not None else ApprovalMode.MANUAL,
    )
    execute = AsyncMock(spec=lambda: None)
    try:
        logger = approval_module._prejudge_logger.name
        with caplog.at_level(logging.INFO, logger=logger):
            await middleware.process(context, execute)
        records = [record.getMessage() for record in caplog.records if record.name == logger]
        if mode == "off":
            assert not records
        else:
            (prediction,) = records
            assert f"{mode} route=defer reason=sensitive_target " in prediction
            assert f"policy_candidate={'allow' if matches else 'defer'} decoded_sensitive_target=true" in prediction
            assert command not in prediction
            assert ".env" not in prediction
        if mode == "enforce":
            execute.assert_not_awaited()
            (request,) = requests
            assert request.reuse_offer is None
            assert request.judging is (judge is not None)
            if judge is not None:
                judge.evaluate.assert_awaited_once()
        else:
            execute.assert_awaited_once()
            assert not requests
            if path in {"legacy-readonly", "daa"}:
                (decision,) = middleware.drain_decisions()
                assert decision["status"] == ("reuse_approved" if path == "daa" else "auto_approved")
            if judge is not None:
                judge.evaluate.assert_not_awaited()
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, answer)
        await bus.unsubscribe(ApprovalReviewed, reject_review)


@pytest.mark.parametrize(
    "command",
    [
        "ls *.py",
        "ls 2>/dev/null",
        "sort f",
    ],
)
async def test_real_parser_empty_policy_keeps_readonly_approval(tmp_path, monkeypatch, command):
    require_grammars("tree_sitter", "tree_sitter_bash")
    from chrys.service.approval.prejudge_v2 import load_policy

    path = tmp_path / "prejudge.yaml"
    path.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: empty\nrules: []\n',
        encoding="utf-8",
    )
    loaded = load_policy(path)
    assert loaded.rules == ()
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
        assert executed
        assert middleware._decisions[-1]["status"] == "auto_approved"
    finally:
        await middleware.close()


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("matches", [False, True])
@pytest.mark.parametrize("quote", ["'", '"'], ids=["literal", "expansion"])
async def test_literal_dollar_argv_preserves_readonly_approval(tmp_path, monkeypatch, mode, matches, quote):
    require_grammars("tree_sitter", "tree_sitter_bash")
    argv = ("grep", "-F", "$DEMO_TOKEN", "README.md")
    command = f"grep -F {quote}$DEMO_TOKEN{quote} README.md"
    rules = (Rule("grep", Route.ALLOW, Matcher("argv_exact", argv)),) if matches else ()
    policy = Policy(mode, "grep-literal", rules)
    monkeypatch.setattr(approval_module, "load_policy", lambda _: policy)
    result = route(command, "bash", policy)
    literal = quote == "'"
    assert result.sensitive is (mode != "off" and not literal)
    assert result.decoded_sensitive_target is False
    assert (result.route is Route.ALLOW) is (mode != "off" and matches and literal)
    await set_fixture_search_path(monkeypatch, tmp_path)
    tool = bound_bash_tool()
    bus = EventBus()
    requests = []

    async def reject(request: ApprovalRequest) -> None:
        requests.append(request)
        await bus.publish(ApprovalResponse(request_id=request.request_id, approved=False))

    await bus.subscribe(ApprovalRequest, reject)
    middleware = ApprovalMiddleware(ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]), bus)
    execute = AsyncMock(spec=lambda: None)
    try:
        await middleware.process(FunctionInvocationContext(tool, {"command": command}), execute)
        (decision,) = middleware.drain_decisions()
        if literal:
            execute.assert_awaited_once()
            assert not requests
            assert decision["status"] == ("prejudge_approved" if mode == "enforce" and matches else "auto_approved")
        else:
            execute.assert_not_awaited()
            assert len(requests) == 1
            assert decision["status"] == "user_rejected"
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, reject)


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("matches", [False, True])
@pytest.mark.parametrize(
    ("command", "head", "detected"),
    [
        ("eval 'echo' '$DEMO_TOKEN'", "eval", "decoded"),
        ("timeout -s TERM 5 bash -c 'echo $DEMO_TOKEN'", "timeout", "decoded"),
        ("bash --noprofile -c 'echo $DEMO_TOKEN'", "bash", "source"),
        ("nice -n 5 /bin/bash -c 'echo $DEMO_TOKEN'", "nice", "decoded"),
        ("xargs -n1 bash -c 'echo $DEMO_TOKEN'", "xargs", "decoded"),
        pytest.param("xargs -0 -r -n 1 bash -c 'echo $DEMO_TOKEN'", "xargs", "decoded", id="xargs-separate"),
        pytest.param("xargs -0rn 1 bash -c 'echo $DEMO_TOKEN'", "xargs", "decoded", id="xargs-grouped"),
        pytest.param("xargs -0rn1 bash -c 'echo $DEMO_TOKEN'", "xargs", "decoded", id="xargs-attached"),
        pytest.param(
            "/usr/bin/xargs -0 -n 1 /bin/bash -c 'echo $DEMO_TOKEN'", "/usr/bin/xargs", "decoded", id="absolute-xargs"
        ),
        pytest.param(
            "/usr/bin/XaRgS -0n1 /bin/BaSh -c 'echo $DEMO_TOKEN'", "/usr/bin/XaRgS", "decoded", id="absolute-alias"
        ),
        pytest.param("xargs -0n1 BASH -c 'echo $DEMO_TOKEN'", "xargs", "decoded", id="nested-shell-alias"),
        pytest.param("XARGS \"$GROUP_FLAGS\" BASH -c 'echo $DEMO_TOKEN'", "XARGS", "unresolved", id="dynamic-alias"),
        pytest.param("TIMEOUT 5 XARGS '-I{}' BASH -c 'echo $DEMO_TOKEN'", "TIMEOUT", "unresolved", id="nested-alias"),
        pytest.param("xargs '--replace={}' bash -c 'echo $DEMO_TOKEN'", "xargs", "unresolved", id="xargs-unresolved"),
        pytest.param(
            "xargs \"$GROUP_FLAGS\" bash -c 'echo $DEMO_TOKEN'", "xargs", "unresolved", id="xargs-dynamic-options"
        ),
        pytest.param(
            "timeout 5 xargs '--replace={}' bash -c 'echo $DEMO_TOKEN'",
            "timeout",
            "unresolved",
            id="nested-unresolved",
        ),
        pytest.param("xargs --version", "xargs", "clear", id="xargs-version"),
        pytest.param("xargs -0rn1 grep -F '$DEMO_TOKEN'", "xargs", "clear", id="xargs-literal"),
    ],
)
async def test_carrier_source_sensitivity_is_enforce_only(monkeypatch, caplog, mode, matches, command, head, detected):
    await _assert_carrier_sensitivity(monkeypatch, caplog, mode, matches, command, head, detected)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the native macOS system xargs filesystem")
@pytest.mark.parametrize("case_alias", [False, True], ids=["canonical", "case-alias"])
async def test_macos_xargs_case_alias_requires_approval(monkeypatch, caplog, case_alias):
    canonical = Path("/usr/bin/xargs")
    alias = canonical.with_name("XARGS")
    assert await asyncio.to_thread(canonical.is_file)
    if not await asyncio.to_thread(alias.exists):
        pytest.skip("The macOS system xargs volume does not resolve a differently cased filename")
    assert await asyncio.to_thread(canonical.samefile, alias)
    head = str(alias if case_alias else canonical)
    await _assert_carrier_sensitivity(
        monkeypatch, caplog, "enforce", False, f"{head} -0n1 /bin/bash -c 'echo $DEMO_TOKEN'", head, "decoded"
    )


async def _assert_carrier_sensitivity(monkeypatch, caplog, mode, matches, command, head, detected):
    require_grammars("tree_sitter", "tree_sitter_bash")
    rules = (Rule("carrier", Route.ALLOW, Matcher("argv_first", (head,))),) if matches else ()
    policy = Policy(mode, "carrier-policy", rules)
    monkeypatch.setattr(approval_module, "load_policy", lambda _: policy)
    observed = route(command, "bash", policy)
    assert observed.route is Route.DEFER
    assert observed.policy_candidate is Route.DEFER

    tool = bound_bash_tool()
    bus = EventBus()
    requests = []

    async def reject(request: ApprovalRequest) -> None:
        requests.append(request)
        await bus.publish(ApprovalResponse(request_id=request.request_id, approved=False))

    await bus.subscribe(ApprovalRequest, reject)
    config = ApprovalConfig(default="auto", overrides={"shell": "auto"})
    middleware = ApprovalMiddleware(ApprovalPolicy(config, tools=[tool]), bus)
    execute = AsyncMock(spec=lambda: None)
    try:
        with caplog.at_level(logging.INFO, logger=approval_module._prejudge_logger.name):
            await middleware.process(FunctionInvocationContext(tool, {"command": command}), execute)
        if detected == "source" or (mode == "enforce" and detected != "clear"):
            assert len(requests) == 1
            (decision,) = middleware.drain_decisions()
            assert decision["status"] == "user_rejected"
            execute.assert_not_awaited()
        else:
            assert not requests
            assert not middleware.drain_decisions()
            execute.assert_awaited_once()
        if mode != "off":
            reason = {"unresolved": "unresolved_carrier", "clear": "opaque_carrier"}.get(detected, "sensitive_target")
            assert f"{mode} route=defer reason={reason}" in caplog.text
            assert f"decoded_sensitive_target={str(detected == 'decoded').lower()}" in caplog.text
            assert f"unresolved_carrier={str(detected == 'unresolved').lower()}" in caplog.text
            assert command not in caplog.text
    finally:
        await middleware.close()

    assert observed.sensitive is (mode != "off" and detected in {"decoded", "source"})
    assert observed.decoded_sensitive_target is (mode != "off" and detected == "decoded")
    assert observed.unresolved_carrier is (mode != "off" and detected == "unresolved")


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

    def load(_, *, report_shadow_failure=False):
        assert report_shadow_failure
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


async def test_shadow_logs_spans_without_command_text(monkeypatch, caplog):
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
        with caplog.at_level(logging.DEBUG, logger=approval_module._prejudge_logger.name):
            await middleware.process(FunctionInvocationContext(tool, {"command": command}), execute)
        assert executed
        info = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
        assert any("shadow route=defer" in line and "spans=((0, 4),)" in line for line in info)
        assert all("command=" not in line for line in info)
        assert "command=" not in caplog.text
        assert command not in caplog.text
        assert "hidden_suffix" not in caplog.text
    finally:
        await middleware.close()


@pytest.fixture
def loopback_otlp_logs():
    from concurrent.futures import ThreadPoolExecutor

    import grpc
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2_grpc import (
        LogsServiceServicer,
        add_LogsServiceServicer_to_server,
    )

    received = []

    class Receiver(LogsServiceServicer):
        def Export(self, request, context):
            received.append(request.SerializeToString())
            return ExportLogsServiceResponse()

    with ThreadPoolExecutor(max_workers=1) as pool:
        server = grpc.server(pool)
        add_LogsServiceServicer_to_server(Receiver(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            yield f"http://127.0.0.1:{port}", received
        finally:
            assert server.stop(0).wait(timeout=5)


async def test_shadow_debug_and_exception_records_are_safe_for_otlp(monkeypatch, caplog, loopback_otlp_logs, tmp_path):
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.observability import setup as otel
    from chrys.foundation.observability.gate import TELEMETRY_GATE

    require_grammars("tree_sitter", "tree_sitter_bash")
    endpoint, received = loopback_otlp_logs
    for name in ("ENDPOINT", "TRACES_ENDPOINT", "METRICS_ENDPOINT", "HEADERS", "LOGS_HEADERS"):
        monkeypatch.delenv(f"OTEL_EXPORTER_OTLP_{name}", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("no_grpc_proxy", "127.0.0.1")
    # The actual handler uses its explicit provider. Avoid changing the SDK's set-once global.
    monkeypatch.setattr("opentelemetry._logs.set_logger_provider", lambda provider: None)
    assert otel.setup_otel(Settings(otel_enabled=True, otel_sensitive_data=False)) is not None
    provider = otel._INSTALLED_LOGGER_PROVIDER
    assert TELEMETRY_GATE.enabled and not TELEMETRY_GATE.sensitive_enabled

    marker = "ICODE_PREJUDGE_SECRET_MARKER_20261124"
    command = f"echo {marker}"
    monkeypatch.setattr(approval_module, "load_policy", lambda _: Policy("shadow", "otel-fixture", ()))
    tool = bound_bash_tool()
    middleware = ApprovalMiddleware(ApprovalPolicy(ApprovalConfig(default="skip"), tools=[tool]), EventBus())
    execute = AsyncMock(spec=lambda: None)

    def fail_with_source(command: str, dialect: str, policy: Policy) -> PreJudgeResult:
        raise ValueError(command)

    def fail_parser_initialization() -> None:
        raise RuntimeError(marker)

    try:
        with caplog.at_level(logging.DEBUG, logger=approval_module._prejudge_logger.name):
            approval_module._prejudge_logger.debug("OTLP DEBUG control")
            await middleware.process(FunctionInvocationContext(tool, {"command": command}), execute)
            monkeypatch.setattr(approval_module, "route", fail_with_source)
            await middleware.process(FunctionInvocationContext(tool, {"command": command}), execute)
            path = tmp_path / "test-prejudge.yaml"
            # Startup can log an exception. Exercise the public error, including its chain.
            await asyncio.to_thread(path.write_text, f'mode: "shadow"\nrules: [{marker}\n', encoding="utf-8")
            with pytest.raises(ValueError, match="Invalid YAML syntax") as failure:
                load_policy(path)
            assert failure.value.__context__ is None
            startup_logger = logging.getLogger(approval_module._prejudge_logger.name)
            startup_logger.warning("Startup failure", exc_info=failure.value)

            header = 'schema_version: 1\nmode: "shadow"\npolicy_id: privacy\n'
            invalid_rule = (
                f"rules:\n  - id: {marker}\n    effect: allow\n    target: command\n"
                f"    match:\n      {marker}: [echo]\n"
            )
            await asyncio.to_thread(path.write_text, header + invalid_rule, encoding="utf-8")
            assert load_policy(path).mode == "off"
            with pytest.raises(ValueError, match="rule 1: Unsupported matcher field") as failure:
                load_policy(path, report_shadow_failure=True)
            assert marker not in str(failure.value)

            monkeypatch.setattr(prejudge_v2, "_language", fail_parser_initialization)
            await asyncio.to_thread(path.write_text, header + "rules: []\n", encoding="utf-8")
            assert load_policy(path).mode == "off"
            with pytest.raises(RuntimeError, match="Parser unavailable") as failure:
                load_policy(path, report_shadow_failure=True)
            assert failure.value.__context__ is None
        assert execute.await_count == 2
        assert [entry["status"] for entry in middleware.drain_decisions()] == ["auto_approved", "auto_approved"]
        assert await asyncio.to_thread(provider.force_flush, timeout_millis=5000)
        data = b"".join(received)
        assert b"OTLP DEBUG control" in data
        assert b"shadow route=defer reason=unmatched_unit policy=otel-fixture" in data
        assert b"shadow pre-judge evaluation failed (ValueError); deferring" in data
        assert b"Invalid YAML syntax" in data
        assert b"rule 1: Unsupported matcher field" in data
        assert b"Parser unavailable" in data
        assert marker.encode() not in data
        assert "shadow pre-judge evaluation failed (ValueError); deferring" in caplog.text
        assert marker not in caplog.text
    finally:
        await middleware.close()
        await asyncio.to_thread(otel._revoke_previous_setup)
        await asyncio.to_thread(provider.shutdown)


async def test_human_edited_command_does_not_reenter_prejudge(monkeypatch):
    monkeypatch.setattr(approval_module, "load_policy", lambda _: Policy("enforce", "pilot", ()))
    routed = []

    def fake_route(command, dialect, policy):
        routed.append(command)
        return PreJudgeResult(
            Route.DEFER,
            "unmatched_unit",
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
