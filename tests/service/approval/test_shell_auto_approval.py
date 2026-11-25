# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The approval boundary must not auto-approve expandable or exec-capable shell text."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import ShellInfo
from chrys.foundation.tool_kinds import KIND_SHELL, set_tool_kind
from chrys.kernel import FunctionTool
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.tools.builtins.shell import ShellTools
from tests.support.waiting import wait_for


def _middleware(bus: EventBus, *, default: str = "require") -> ApprovalMiddleware:
    overrides = {"shell": "skip"} if default == "skip" else None
    config = (
        ApprovalConfig(default=default) if overrides is None else ApprovalConfig(default=default, overrides=overrides)
    )
    return ApprovalMiddleware(
        ApprovalPolicy(config),
        bus,
        approval_mode=ApprovalMode.MANUAL,
    )


def _shell_tool():
    runtime = SessionEnvironment.capture()
    shell = ShellTools(runtime)
    # Keep this contract test independent of the developer's detected shell;
    # Windows fail-closed behavior has its own classifier test.
    shell._shell = ShellInfo(name="bash", path="bash", args=["-c"])
    return shell.tools()[0]


def _fake_custom_bash(command: str) -> str:
    return command


def _fake_list_workspace_dirs() -> str:
    return ""


def _custom_shell_tool(name: str, func: Callable[..., str]) -> FunctionTool:
    tool = FunctionTool(func=func, name=name, description="Fake shell-kind tool")
    set_tool_kind(tool, KIND_SHELL)
    return tool


async def _record_requests(bus: EventBus) -> list[ApprovalRequest]:
    requests: list[ApprovalRequest] = []

    async def record(event: ApprovalRequest) -> None:
        requests.append(event)

    await bus.subscribe(ApprovalRequest, record)
    return requests


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat README.md",
        "grep TODO src/chrys/service",
        "git log --oneline",
        "echo hello",
    ],
)
async def test_safe_shell_commands_skip_the_dialog(command: str) -> None:
    bus = EventBus()
    requests = await _record_requests(bus)
    middleware = _middleware(bus)
    called = AsyncMock()

    await middleware.process(FunctionInvocationContext(_shell_tool(), {"command": command}), called)

    called.assert_awaited_once()
    assert requests == []


async def test_renamed_shell_tool_requires_approval() -> None:
    """Approval and execution must agree on the real shell, not just a display name."""
    bus = EventBus()
    requests = await _record_requests(bus)
    middleware = _middleware(bus, default="skip")
    called = AsyncMock()
    tool = _shell_tool()
    actual_shell = tool.bound_instance.shell.name
    tool.name = "bash" if actual_shell != "bash" else "zsh"

    task = asyncio.create_task(middleware.process(FunctionInvocationContext(tool, {"command": "ls -la"}), called))
    await wait_for(lambda: len(requests) == 1, description="renamed shell approval request")
    assert called.await_count == 0

    await bus.publish(ApprovalResponse(request_id=requests[0].request_id, approved=False, reason="test"))
    await task

    assert called.await_count == 0


async def test_custom_kind_shell_tools_are_not_treated_as_renamed_executors() -> None:
    """An extension or test double has no bound shell identity; only ShellTools does."""
    bus = EventBus()
    requests = await _record_requests(bus)
    middleware = _middleware(bus, default="skip")
    called = AsyncMock()
    tool = _custom_shell_tool("fake_bash", _fake_custom_bash)

    await middleware.process(FunctionInvocationContext(tool, {"command": "ls -la"}), called)

    called.assert_awaited_once()
    assert requests == []


async def test_kind_shell_tool_without_a_command_skips_shell_identity_checks() -> None:
    """``list_workspace_dirs`` shares KIND_SHELL but has no command to approve."""
    bus = EventBus()
    requests = await _record_requests(bus)
    middleware = _middleware(bus, default="skip")
    called = AsyncMock()
    tool = _custom_shell_tool("list_workspace_dirs", _fake_list_workspace_dirs)

    await middleware.process(FunctionInvocationContext(tool, {}), called)

    called.assert_awaited_once()
    assert requests == []


@pytest.mark.parametrize("command", ["cat .en''v", "rg secret .en''v", "env A=", "'env'", "cat café.txt .en''v"])
async def test_permissive_policy_still_requires_approval_for_normalized_sensitive_targets(
    command: str,
) -> None:
    """A policy that allows shell must not bypass sensitive targets hidden by quote concatenation."""
    bus = EventBus()
    requests = await _record_requests(bus)
    middleware = _middleware(bus, default="skip")
    called = AsyncMock()

    task = asyncio.create_task(
        middleware.process(FunctionInvocationContext(_shell_tool(), {"command": command}), called)
    )
    await wait_for(lambda: len(requests) == 1, description="normalized sensitive approval request")
    assert called.await_count == 0

    await bus.publish(ApprovalResponse(request_id=requests[0].request_id, approved=False, reason="test"))
    await task

    assert called.await_count == 0


@pytest.mark.parametrize(
    "command",
    [
        "rg --pre ./x.sh -e y .",
        "rg --pre=./x.sh -e y .",
        "rg --pre$IFS./x.sh -e y .",
        "rg --p''re ./x.sh -e y .",
        "r''g --pre ./x.sh -e y .",
        "\\rg --pre ./x.sh -e y .",
        "rg -n TODO .",
        "cat .env*",
        "cat .env",
        "cat .en''v",
        "cat .gi''t/con''fig",
        "cat .ss''h/id_r''sa",
        "cat /proc/self/environ",
        "cat /proc/self/envi''ron",
        "env A=",
        "en''v",
        "'env'",
        "pytest --help",
        "pip list --log out.log",
        "uniq -- input.txt output.txt",
        "git status --short",
        "tree -o out .",
        "date -s 2020-01-01",
        "hostname evil",
        "less README.md",
    ],
)
async def test_executable_or_expandable_shell_commands_require_the_dialog(command: str) -> None:
    bus = EventBus()
    requests = await _record_requests(bus)
    middleware = _middleware(bus)
    called = AsyncMock()

    task = asyncio.create_task(
        middleware.process(FunctionInvocationContext(_shell_tool(), {"command": command}), called)
    )
    await wait_for(lambda: len(requests) == 1, description="shell approval request")
    assert called.await_count == 0

    await bus.publish(ApprovalResponse(request_id=requests[0].request_id, approved=False, reason="test"))
    await task

    assert called.await_count == 0
    assert requests[0].args["command"] == command
