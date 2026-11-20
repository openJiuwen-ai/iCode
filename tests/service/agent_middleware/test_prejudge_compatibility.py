# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pre-judge deferral preserves existing approval and fails closed on mode changes."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse
from chrys.foundation.platform import get_platform
from chrys.foundation.tool_kinds import KIND_SHELL, set_tool_kind
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.kernel.tools import FunctionTool
from chrys.service.agent_middleware.control import approval as approval_module
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval import prejudge_v2
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.tools.builtins import shell_filter


def _write_policy() -> Path:
    path = get_platform().config_dir / "prejudge.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: compatibility\n'
        "rules:\n  - id: configured-version\n    effect: allow\n    target: command\n"
        "    match:\n      argv_exact: [configured_tool, --version]\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def allow_policy() -> Path:
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_bash")
    return _write_policy()


@pytest.mark.parametrize(
    ("shell", "command", "readonly"),
    [
        ("bash", "ls *.py", True),
        ("bash", "ls 2>/dev/null", True),
        ("bash", "sed -n 1,10p f", True),
        ("bash", "sort f", True),
        ("bash", "uniq -c f", True),
        ("bash", "cat file.txt | head -20 | grep pattern", True),
        ("bash", "git --no-optional-locks status --short", True),
        ("bash", "git status --short", False),
        ("bash", "git push origin main", False),
        ("bash", "find . -delete", False),
        ("bash", "git log --oneline -5", True),
        ("bash", "git log --no-ext-diff --no-textconv -p -1", True),
        ("bash", "git log -p -1", False),
        ("bash", "git log --oneline --output=history.txt -5", False),
        ("bash", "awk '{print $1}' file.txt", True),
        ("bash", "awk '{ print $12 }'", True),
        ("bash", "awk '{print $1}' -f script.awk", False),
        ("bash", "awk '{print $1}' --source='BEGIN {system(\"unsafe_tool\")}'", False),
        ("bash", "python3 --version", True),
        ("bash", "python3 script.py", False),
        ("bash", "pip list", True),
        ("bash", "pip install package", False),
        ("bash", "ruff check --no-cache --no-fix src tests", True),
        ("bash", "ruff check --fix src", False),
        ("bash", "ls > output.txt", False),
        ("bash", "echo $(python3 script.py)", False),
        ("bash", "echo rm", False),
        ("bash", "echo 'find -delete is dangerous'", True),
        ("git_bash", "ls *.py", True),
        ("zsh", "ls", True),
        ("sh", "ls", True),
        ("dash", "ls", True),
        ("powershell", "Get-ChildItem", True),
        ("pwsh", "Get-ChildItem", True),
        ("cmd", "dir", True),
    ],
)
async def test_defer_keeps_legacy_readonly_approval(
    allow_policy: Path, shell: str, command: str, readonly: bool
) -> None:
    assert shell_filter.is_safe_readonly_command(command, shell_name=shell) is readonly
    policy = prejudge_v2.load_policy(allow_policy)
    assert prejudge_v2.route(command, shell, policy).route is prejudge_v2.Route.DEFER
    tool = FunctionTool(name=shell)
    set_tool_kind(tool, KIND_SHELL)
    bus = EventBus()
    requests: list[ApprovalRequest] = []

    async def reject(request: ApprovalRequest) -> None:
        requests.append(request)
        await bus.publish(ApprovalResponse(request_id=request.request_id, approved=False))

    await bus.subscribe(ApprovalRequest, reject)
    middleware = ApprovalMiddleware(ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]), bus)
    context = FunctionInvocationContext(tool, {"command": command})
    executed = False

    async def execute() -> None:
        nonlocal executed
        executed = True

    try:
        await middleware.process(context, execute)
        assert executed is readonly
        assert len(requests) == (0 if readonly else 1)
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, reject)


async def test_structured_allow_precedes_legacy_readonly(allow_policy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_readonly(command: str, *, shell_name: str = "") -> bool:
        pytest.fail("An eligible structured ALLOW must return before the legacy readonly shortcut")

    monkeypatch.setattr(shell_filter, "is_safe_readonly_command", forbidden_readonly)
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    middleware = ApprovalMiddleware(ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]), EventBus())
    context = FunctionInvocationContext(tool, {"command": "configured_tool --version"})
    executed = False

    async def execute() -> None:
        nonlocal executed
        executed = True

    try:
        await middleware.process(context, execute)
        assert executed
        assert middleware._decisions[-1]["status"] == "prejudge_approved"
    finally:
        await middleware.close()


async def test_readonly_fallback_keeps_sensitive_data_approval(allow_policy: Path) -> None:
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    bus = EventBus()
    requests: list[ApprovalRequest] = []

    async def reject(request: ApprovalRequest) -> None:
        requests.append(request)
        await bus.publish(ApprovalResponse(request_id=request.request_id, approved=False))

    await bus.subscribe(ApprovalRequest, reject)
    middleware = ApprovalMiddleware(ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]), bus)

    async def execute() -> None:
        pytest.fail("A sensitive read must wait for approval")

    try:
        await middleware.process(FunctionInvocationContext(tool, {"command": "cat .env"}), execute)
        assert len(requests) == 1
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, reject)


@pytest.mark.parametrize("mode", [ApprovalMode.MANUAL, ApprovalMode.AUTO])
@pytest.mark.parametrize("failure", ["invalid_rule", "invalid_yaml", "missing_parser"])
async def test_leaving_bypass_with_bad_policy_requires_ordinary_approval(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mode: ApprovalMode, failure: str
) -> None:
    path = _write_policy()
    if failure == "invalid_yaml":
        path.write_text('mode: "enforce"\nrules: [\n', encoding="utf-8")
    elif failure == "invalid_rule":
        path.write_text(path.read_text(encoding="utf-8").replace("effect: allow", "effect: deny"), encoding="utf-8")

    def ensure_parser() -> None:
        if failure == "missing_parser":
            raise RuntimeError("tree-sitter not installed")

    monkeypatch.setattr(prejudge_v2, "_ensure_bash_parser", ensure_parser)
    tool = FunctionTool(name="bash")
    set_tool_kind(tool, KIND_SHELL)
    policy = ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool])
    bus = EventBus()
    requests: list[ApprovalRequest] = []

    async def reject(request: ApprovalRequest) -> None:
        requests.append(request)
        await bus.publish(ApprovalResponse(request_id=request.request_id, approved=False))

    await bus.subscribe(ApprovalRequest, reject)
    first = ApprovalMiddleware(policy, bus, approval_mode=ApprovalMode.BYPASS)
    second = ApprovalMiddleware(policy, bus, approval_mode=ApprovalMode.BYPASS)
    middlewares = [first, second]

    async def execute() -> None:
        pytest.fail("Failed policy initialization must not authorize a call")

    try:
        with caplog.at_level(logging.ERROR, logger=approval_module.__name__ + ".prejudge"):
            first.set_approval_mode(mode)
        assert first.approval_mode is mode
        assert str(path) in caplog.text
        second.set_approval_mode(mode)
        middlewares.append(ApprovalMiddleware(policy, bus, approval_mode=mode))
        for middleware in middlewares:
            assert middleware.approval_mode is mode
            for command in ("ls", "rm -rf build"):
                await middleware.process(FunctionInvocationContext(tool, {"command": command}), execute)
        assert len(requests) == 6
    finally:
        for middleware in middlewares:
            await middleware.close()
        await bus.unsubscribe(ApprovalRequest, reject)
